"""/v1/portfolios: portfolios, their positions, orders and fills, manual orders, and broker sync."""

import csv
import io
import sqlite3
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..budgets import store as budget
from ..brokers import store as brokers_store
from ..brokers.base import BrokerError
from ..db import immediate
from ..filters import store as filters_store
from ..deps import AppContext
from ..market.client import LIVE_SOURCES, MarketError
from ..orders import service, store as orders_store
from . import store
from .store import D0, num


class PortfolioIn(BaseModel):
    """Body of POST /v1/portfolios.

    Attributes:
        name: display name.
        description: description.
        brokerAccountId: the broker account holding its money.
        capital: initial capital, taken from the account's unassigned cash.

    Used by: router.create_portfolio.
    """
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    brokerAccountId: str
    capital: Decimal = Field(default=Decimal(0), ge=0, decimal_places=2)  # taken from the account's unassigned cash


class CapitalChangeIn(BaseModel):
    """Body of the capital allocate / withdraw routes.

    Attributes:
        amount: positive amount, 2 decimals.
        note: free text for the ledger.

    Used by: router.allocate, router.withdraw.
    """
    amount: Decimal = Field(gt=0, decimal_places=2)
    note: str | None = None


class PortfolioUpdate(BaseModel):
    """Body of PUT /v1/portfolios/{id}.

    Attributes:
        name: display name.
        description: description.

    Used by: router.update_portfolio.
    """
    name: str = Field(min_length=1, max_length=100)
    description: str = ""


class ManualOrderIn(BaseModel):
    """Body of POST /v1/portfolios/{id}/orders (a manual order).

    Attributes:
        symbol: ticker (must be in the halal universe).
        side: BUY or SELL.
        type: MARKET, LIMIT, STOP, STOP_LIMIT or TRAILING_STOP.
        quantity: shares; fractions only for MARKET + DAY.
        timeInForce: DAY, GTC, GTD, OPG, CLS, IOC or FOK (the broker may support fewer).
        limitPrice: for LIMIT / STOP_LIMIT.
        stopPrice: for STOP / STOP_LIMIT.
        trailPercent: for TRAILING_STOP.
        extendedHours: allow filling outside regular hours (LIMIT + DAY).
        budgetId: trade with a budget of this portfolio: a buy uses its money, a sell its shares.

    Used by: router.place_manual_order.
    """
    symbol: str
    side: Literal["BUY", "SELL"]
    type: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP"] = "MARKET"
    quantity: Decimal = Field(gt=0)
    timeInForce: Literal["DAY", "GTC", "GTD", "OPG", "CLS", "IOC", "FOK"] = "DAY"
    limitPrice: Decimal | None = Field(default=None, gt=0)
    stopPrice: Decimal | None = Field(default=None, gt=0)
    trailPercent: Decimal | None = Field(default=None, gt=0)
    extendedHours: bool = False
    budgetId: str | None = None  # trade with a budget of this portfolio: its money / its shares


def router(ctx: AppContext) -> APIRouter:
    """The /v1/portfolios routes.

    Args:
        ctx: app context (DB, market helpers, broker factory, key check).

    Used by: api.create_app.
    """
    r = APIRouter(prefix="/v1/portfolios", tags=["portfolios"], dependencies=[Depends(ctx.require_key)])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]

    def load(conn, portfolio_id: str) -> dict:
        """A stored portfolio, or 404.

        Args:
            conn: app DB connection.
            portfolio_id: the portfolio's id.
        """
        p = store.get_portfolio(conn, portfolio_id)
        if not p:
            raise HTTPException(404, f"portfolio '{portfolio_id}' not found")
        return p

    def account_of(conn, portfolio: dict) -> dict:
        """The portfolio's broker account.

        Args:
            conn: app DB connection.
            portfolio: the portfolio dict.
        """
        return brokers_store.get_account(conn, portfolio["brokerAccountId"])

    def broker_for(conn, portfolio: dict):
        """The portfolio's Broker, or 502.

        Args:
            conn: app DB connection.
            portfolio: the portfolio dict.
        """
        try:
            return ctx.broker_factory(account_of(conn, portfolio))
        except BrokerError as e:
            raise HTTPException(502, str(e))

    def run_sync(conn, account: dict) -> dict:
        """Sync a broker account, or 502.

        Args:
            conn: app DB connection.
            account: the broker account dict.

        Returns:
            The sync record.
        """
        try:
            return service.run_account_sync(conn, ctx.broker_factory, account)
        except BrokerError as e:
            raise HTTPException(502, f"broker sync failed: {e}")

    def unassigned_cash(conn, account: dict) -> Decimal:
        """Account cash not given to any portfolio (balances from the last sync).

        Args:
            conn: app DB connection.
            account: the broker account dict.
        """
        return Decimal(str(account["cash"] or 0)) - store.assigned_cash(conn, account["id"])

    def summary(conn, p: dict, live: bool = False) -> dict:
        """Portfolio with positions valued at the latest prices, budgets, totals and the account's last sync.

        Args:
            conn: app DB connection.
            p: the portfolio dict.
            live: value the positions at the live last trade (IBKR, through the market service) where available;
                else the delayed prices only.
        """
        rows = store.positions(conn, p["id"], by_owner=True)
        symbols = {x["symbol"] for x in rows}
        try:
            snaps = ctx.market.snapshots(symbols)
        except MarketError:
            snaps = {}
        quotes = {}
        if live and symbols:
            try:
                # Only quotes with a last trade: without one (often before the open) the price is the bid/ask
                # midpoint, which is far off on a wide spread; the delayed last trade is closer to the broker's value.
                quotes = {t: q for t, q in ctx.market.live_quotes(symbols).items()
                          if q.get("source") in LIVE_SOURCES and q.get("last") is not None}
            except MarketError:
                quotes = {}

        def pct(gain: Decimal, base: Decimal) -> float | None:
            return round(float(gain / base * 100), 2) if base else None

        positions, value_total, cost_total, unrealized = [], D0, D0, D0
        for x in rows:
            q = quotes.get(x["symbol"])
            # Valued at the last trade, like the broker shows it (the bid/ask midpoint drifts after the close).
            price = q["last"] if q else snaps.get(x["symbol"], {}).get("latestPrice")
            value = x["quantity"] * Decimal(str(price)) if price is not None else None
            if value is not None:
                value_total += value
                cost_total += x["costBasis"]  # only priced positions, so cost and value compare like for like
                unrealized += value - x["costBasis"]
            positions.append({"symbol": x["symbol"], "budgetId": x["budgetId"], "automationId": x["automationId"],
                              "quantity": num(x["quantity"]),
                              "averageCost": num(x["averageCost"]), "costBasis": num(x["costBasis"]),
                              "price": price, "priceSource": q["source"] if q else "DELAYED", "marketValue": num(value),
                              "unrealizedPnL": num(value - x["costBasis"]) if value is not None else None,
                              "unrealizedPct": pct(value - x["costBasis"], x["costBasis"]) if value is not None else None})
        manual = service.open_manual_buys(conn, p["id"])
        account = account_of(conn, p)
        equity = Decimal(str(p["cash"])) + value_total
        total_return = equity - Decimal(str(p["allocated"]))
        return {**p, "unallocatedCash": num(budget.unallocated_cash(conn, p, manual)), "openManualBuys": num(manual),
                "positionsValue": num(value_total), "positionsCost": num(cost_total), "unrealizedPnL": num(unrealized),
                "unrealizedPct": pct(unrealized, cost_total),
                "equity": num(equity), "totalReturn": num(total_return),
                "totalReturnPct": pct(total_return, Decimal(str(p["allocated"]))),
                "realizedPnL": num(store.realized_pnl(conn, p["id"])), "budgets": budget.list_budgets(conn, p["id"]),
                "positions": positions,
                "account": {"id": account["id"], "name": account["name"], "cash": account["cash"],
                            "unassignedCash": num(unassigned_cash(conn, account))},
                "lastSync": orders_store.last_sync(conn, account["id"])}

    @r.get("")
    def list_portfolios(conn: Conn):
        """Every portfolio (stored values; no prices).
        \f
        Args:
            conn: app DB connection (injected).
        """
        return {"portfolios": store.list_portfolios(conn)}

    @r.post("", status_code=201)
    def create_portfolio(body: PortfolioIn, conn: Conn):
        """A broker account can hold several portfolios. `capital` comes from the account's unassigned cash.
        Shares already in the account stay unassigned; only this portfolio's own orders fill it.
        \f
        Args:
            body: the portfolio.
            conn: app DB connection (injected).
        """
        account = brokers_store.get_account(conn, body.brokerAccountId)
        if not account:
            raise HTTPException(422, f"broker account '{body.brokerAccountId}' does not exist")
        run_sync(conn, account)  # fresh balances
        account = brokers_store.get_account(conn, account["id"])
        with immediate(conn):  # check and create under the write lock
            free = unassigned_cash(conn, account)
            if body.capital > free:
                raise HTTPException(422, f"only {free:.2f} {account['baseCurrency']} of the account is unassigned")
            p = store.create_portfolio(conn, body.name, body.description, account, body.capital)
        return summary(conn, p)

    @r.get("/{portfolio_id}")
    def get_portfolio(portfolio_id: str, conn: Conn):
        """The portfolio with positions at live prices where available (`priceSource: IBKR`, else the newest minute bar,
        price), budgets, totals and the last broker sync.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            conn: app DB connection (injected).
        """
        return summary(conn, load(conn, portfolio_id), live=True)

    @r.put("/{portfolio_id}")
    def update_portfolio(portfolio_id: str, body: PortfolioUpdate, conn: Conn):
        """Change the name and description.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            body: name and description.
            conn: app DB connection (injected).
        """
        load(conn, portfolio_id)
        return store.update_portfolio(conn, portfolio_id, body.name, body.description)

    @r.delete("/{portfolio_id}", status_code=204)
    def delete_portfolio(portfolio_id: str, conn: Conn):
        """Only an empty portfolio: no shares, no open orders, no budgets, no filter using it. Its cash goes back
        to unassigned.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            conn: app DB connection (injected).
        """
        p = load(conn, portfolio_id)
        if store.positions(conn, portfolio_id):
            raise HTTPException(409, "the portfolio still holds shares")
        if orders_store.list_orders(conn, portfolio_id, open_only=True, limit=1):
            raise HTTPException(409, "the portfolio has open orders")
        budgets = budget.list_budgets(conn, portfolio_id)
        if budgets:
            raise HTTPException(409, f"delete its budgets first: {', '.join(b['name'] for b in budgets)}")
        users = filters_store.portfolio_users(conn, portfolio_id)
        if users:
            raise HTTPException(409, f"used by filter: {', '.join(users)}")
        store.delete_portfolio(conn, p["id"])

    @r.post("/{portfolio_id}/capital/allocate")
    def allocate(portfolio_id: str, body: CapitalChangeIn, conn: Conn):
        """Move money from the broker account's unassigned cash into the portfolio.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            body: amount and note.
            conn: app DB connection (injected).
        """
        p = load(conn, portfolio_id)
        account = account_of(conn, p)
        with immediate(conn):  # check and change under the write lock
            free = unassigned_cash(conn, account)
            if body.amount > free:
                raise HTTPException(422, f"only {free:.2f} of the account is unassigned")
            store.change_cash(conn, portfolio_id, "ALLOCATE", body.amount, allocated=body.amount, note=body.note)
        return summary(conn, store.get_portfolio(conn, portfolio_id), live=True)

    @r.post("/{portfolio_id}/capital/withdraw")
    def withdraw(portfolio_id: str, body: CapitalChangeIn, conn: Conn):
        """Give unallocated portfolio cash (not in budgets, not held by open buys) back to the account.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            body: amount and note.
            conn: app DB connection (injected).
        """
        p = load(conn, portfolio_id)
        with immediate(conn):  # check and change under the write lock
            free = budget.unallocated_cash(conn, p)
            if body.amount > free:
                raise HTTPException(422, f"only {free:.2f} is unallocated in this portfolio")
            store.change_cash(conn, portfolio_id, "WITHDRAW", -body.amount, allocated=-body.amount, note=body.note)
        return summary(conn, store.get_portfolio(conn, portfolio_id), live=True)

    @r.get("/{portfolio_id}/ledger")
    def ledger(portfolio_id: str, conn: Conn, limit: Annotated[int, Query(ge=1, le=1000)] = 200):
        """Every change of the portfolio's cash, newest first.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            conn: app DB connection (injected).
            limit: how many to return, newest first (query).
        """
        load(conn, portfolio_id)
        return {"entries": store.entries(conn, portfolio_id, limit)}

    @r.get("/{portfolio_id}/sales", response_class=PlainTextResponse)
    def sales(portfolio_id: str, conn: Conn, year: Annotated[int, Query(ge=2000, le=2100)]):
        """Every lot sold in the year as CSV, for tax: sell and buy date (UTC), shares, prices, cost, proceeds, gain
        in USD, and the budget. The broker's account fees and dividends are in the ledger and /dividends.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            conn: app DB connection (injected).
            year: calendar year of the sell date (query).
        """
        load(conn, portfolio_id)
        names = {b["id"]: b["name"] for b in budget.list_budgets(conn, portfolio_id)}
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["sold_at", "symbol", "quantity", "bought_at", "buy_price", "sell_price", "cost_usd", "proceeds_usd",
                    "gain_usd", "budget"])
        for x in store.sales(conn, portfolio_id, year):
            w.writerow([x["soldAt"], x["symbol"], x["quantity"], x["boughtAt"], x["buyPrice"], x["sellPrice"],
                        round(x["cost"], 2), round(x["proceeds"], 2), round(x["gain"], 2),
                        names.get(x["budgetId"], x["budgetId"] or "manual")])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="sales-{year}.csv"'})

    @r.get("/{portfolio_id}/dividends")
    def dividends(portfolio_id: str, conn: Conn, year: Annotated[int, Query(ge=2000, le=2100)]):
        """Dividends credited to the portfolio in a year, per symbol: received, tax withheld, net. For purification
        and tax records.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            conn: app DB connection (injected).
            year: calendar year of the pay date (query).
        """
        load(conn, portfolio_id)
        rows = orders_store.dividend_report(conn, portfolio_id, year)
        return {"year": year, "symbols": rows, "received": round(sum(r["received"] for r in rows), 2),
                "withheld": round(sum(r["withheld"] for r in rows), 2), "net": round(sum(r["net"] for r in rows), 2)}

    @r.post("/{portfolio_id}/sync")
    def sync_now(portfolio_id: str, conn: Conn):
        """Sync the portfolio's broker account now (normally every minute).
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            conn: app DB connection (injected).
        """
        p = load(conn, portfolio_id)
        result = run_sync(conn, account_of(conn, p))
        return {"sync": result, "portfolio": summary(conn, store.get_portfolio(conn, portfolio_id), live=True)}

    @r.get("/{portfolio_id}/orders")
    def orders(portfolio_id: str, conn: Conn, status: str | None = None,
               automation_id: Annotated[str | None, Query(alias="automationId")] = None,
               open_only: Annotated[bool, Query(alias="openOnly")] = False,
               limit: Annotated[int, Query(ge=1, le=1000)] = 100,
               budget_id: Annotated[str | None, Query(alias="budgetId")] = None):
        """The portfolio's orders, newest first.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            conn: app DB connection (injected).
            status: only this status (query).
            automation_id: only this automation's (query automationId).
            open_only: only open orders (query openOnly).
            limit: how many to return, newest first (query).
            budget_id: only orders of this budget (query budgetId).
        """
        load(conn, portfolio_id)
        return {"orders": orders_store.list_orders(conn, portfolio_id, automation_id, status, open_only, limit,
                                                   budget_id=budget_id)}

    @r.get("/{portfolio_id}/fills")
    def fills(portfolio_id: str, conn: Conn, limit: Annotated[int, Query(ge=1, le=1000)] = 100):
        """The portfolio's fills, newest first.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            conn: app DB connection (injected).
            limit: how many to return, newest first (query).
        """
        load(conn, portfolio_id)
        return {"fills": orders_store.list_fills(conn, portfolio_id, limit=limit)}

    @r.get("/{portfolio_id}/lots")
    def lots(portfolio_id: str, conn: Conn, open_only: Annotated[bool, Query(alias="openOnly")] = True):
        """The portfolio's position lots, oldest first.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            conn: app DB connection (injected).
            open_only: only lots with shares left (query openOnly, default true).
        """
        load(conn, portfolio_id)
        sql = "SELECT * FROM position_lots WHERE portfolio_id = ?" + (" AND closed_at IS NULL" if open_only else "")
        return {"lots": [store.lot_json(x) for x in conn.execute(sql + " ORDER BY opened_at, rowid", (portfolio_id,))]}

    @r.post("/{portfolio_id}/orders", status_code=201)
    def place_manual_order(portfolio_id: str, body: ManualOrderIn, conn: Conn):
        """Place an order by hand. Only halal symbols, except selling shares of a symbol that left the universe.
        With budgetId, a buy uses that budget's money and a sell its shares; without it, unallocated cash and manual
        shares.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            body: the order.
            conn: app DB connection (injected).
        """
        p = load(conn, portfolio_id)
        symbol = body.symbol.upper()
        try:
            if symbol in ctx.market.universe():
                snap = ctx.market.snapshots({symbol}).get(symbol, {})
                quote = ctx.market.live_quotes({symbol}).get(symbol, {})
            elif body.side == "SELL" and symbol in {r["ticker"] for r in ctx.market_api.removed_symbols()["symbols"]}:
                snap, quote = {}, {}  # held shares of a symbol that left the universe can be sold; no prices for it
            else:
                raise HTTPException(422, f"{symbol} is not in the halal universe")
        except MarketError as e:
            raise HTTPException(502, e.detail)
        estimate = snap.get("latestPrice")
        if quote.get("source") in LIVE_SOURCES:  # live: buys at the ask, sells at the bid
            estimate = (quote.get("ask") if body.side == "BUY" else quote.get("bid")) or quote.get("price")
        if body.budgetId:
            b = budget.get_budget(conn, body.budgetId)
            if not b or b["portfolioId"] != portfolio_id:
                raise HTTPException(422, f"budget '{body.budgetId}' is not a budget of this portfolio")
        try:
            return service.place_order(
                conn, broker_for(conn, p), p, symbol=symbol, side=body.side, type=body.type, quantity=body.quantity,
                time_in_force=body.timeInForce, limit_price=body.limitPrice, stop_price=body.stopPrice,
                trail_percent=body.trailPercent, extended_hours=body.extendedHours, budget_id=body.budgetId,
                estimated_price=Decimal(str(estimate)) if estimate is not None else None)
        except service.OrderRejected as e:
            raise HTTPException(422, f"{e.reason}: {e}")

    return r
