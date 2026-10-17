"""/v1/portfolios: portfolios, their positions, orders and fills, manual orders, and broker sync."""

import sqlite3
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..automations import budget
from ..brokers import store as brokers_store
from ..brokers.base import BrokerError
from ..deps import AppContext
from ..market.client import MarketError
from ..orders import service, store as orders_store
from . import store
from .store import D0, num


class PortfolioIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    brokerAccountId: str


class PortfolioUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""


class ManualOrderIn(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    type: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP"] = "MARKET"
    quantity: Decimal = Field(gt=0)
    timeInForce: Literal["DAY", "GTC", "GTD", "OPG", "CLS", "IOC", "FOK"] = "DAY"
    limitPrice: Decimal | None = Field(default=None, gt=0)
    stopPrice: Decimal | None = Field(default=None, gt=0)
    trailPercent: Decimal | None = Field(default=None, gt=0)
    extendedHours: bool = False
    automationId: str | None = None  # trade on behalf of an automation: its budget / its shares


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter(prefix="/v1/portfolios", tags=["portfolios"], dependencies=[Depends(ctx.require_key)])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]

    def load(conn, portfolio_id: str) -> dict:
        p = store.get_portfolio(conn, portfolio_id)
        if not p:
            raise HTTPException(404, f"portfolio '{portfolio_id}' not found")
        return p

    def broker_for(conn, portfolio: dict):
        account = brokers_store.get_account(conn, portfolio["brokerAccountId"])
        try:
            return ctx.broker_factory(account)
        except BrokerError as e:
            raise HTTPException(502, str(e))

    def run_sync(conn, portfolio: dict) -> dict:
        try:
            result = service.sync_portfolio(conn, broker_for(conn, portfolio), portfolio)
            brokers_store.set_status(conn, portfolio["brokerAccountId"], "CONNECTED", synced=True)
            return result
        except BrokerError as e:
            brokers_store.set_status(conn, portfolio["brokerAccountId"], "ERROR", str(e))
            orders_store.add_sync(conn, portfolio["brokerAccountId"], portfolio["id"], 0, 0, [], str(e))
            raise HTTPException(502, f"broker sync failed: {e}")

    def summary(conn, p: dict) -> dict:
        """Portfolio with positions valued at the latest prices, budgets and the last sync."""
        rows = store.positions(conn, p["id"], by_owner=True)
        try:
            snaps = ctx.market.snapshots({x["symbol"] for x in rows})
        except MarketError:
            snaps = {}
        positions = []
        for x in rows:
            price = snaps.get(x["symbol"], {}).get("latestPrice")
            value = x["quantity"] * Decimal(str(price)) if price is not None else None
            positions.append({"symbol": x["symbol"], "automationId": x["automationId"], "quantity": num(x["quantity"]),
                              "averageCost": num(x["averageCost"]), "costBasis": num(x["costBasis"]),
                              "price": price, "marketValue": num(value),
                              "unrealizedPnL": num(value - x["costBasis"]) if value is not None else None})
        pools = [budget.pool_json(row) for row in
                 conn.execute("SELECT * FROM budget_pools WHERE portfolio_id = ?", (p["id"],))]
        manual = service.open_manual_buys(conn, p["id"])
        return {**p, "unallocatedCash": num(budget.unallocated_cash(conn, p, manual)), "openManualBuys": num(manual),
                "budgets": pools, "positions": positions, "realizedPnL": num(store.realized_pnl(conn, p["id"])),
                "lastSync": orders_store.last_sync(conn, p["id"])}

    @r.get("")
    def list_portfolios(conn: Conn):
        return {"portfolios": store.list_portfolios(conn)}

    @r.post("", status_code=201)
    def create_portfolio(body: PortfolioIn, conn: Conn):
        """One portfolio per broker account. Positions already in the account are adopted as manual
        (opening) shares; only fills after this moment are imported."""
        account = brokers_store.get_account(conn, body.brokerAccountId)
        if not account:
            raise HTTPException(422, f"broker account '{body.brokerAccountId}' does not exist")
        if conn.execute("SELECT 1 FROM portfolios WHERE broker_account_id = ?", (account["id"],)).fetchone():
            raise HTTPException(409, "this broker account already has a portfolio")
        try:
            broker = ctx.broker_factory(account)
            opening = broker.positions()
        except BrokerError as e:
            raise HTTPException(502, f"cannot reach the broker: {e}")
        p = store.create_portfolio(conn, body.name, body.description, account)
        with conn:
            for pos in opening:
                if pos.quantity > 0:
                    store.open_lot(conn, p["id"], pos.symbol, None, pos.quantity, pos.average_cost, source="OPENING")
        run_sync(conn, p)
        return summary(conn, store.get_portfolio(conn, p["id"]))

    @r.get("/{portfolio_id}")
    def get_portfolio(portfolio_id: str, conn: Conn):
        return summary(conn, load(conn, portfolio_id))

    @r.put("/{portfolio_id}")
    def update_portfolio(portfolio_id: str, body: PortfolioUpdate, conn: Conn):
        load(conn, portfolio_id)
        return store.update_portfolio(conn, portfolio_id, body.name, body.description)

    @r.post("/{portfolio_id}/sync")
    def sync_now(portfolio_id: str, conn: Conn):
        """Import balances, fills and order updates from the broker now (normally every minute)."""
        p = load(conn, portfolio_id)
        return {"sync": run_sync(conn, p), "portfolio": summary(conn, store.get_portfolio(conn, portfolio_id))}

    @r.get("/{portfolio_id}/orders")
    def orders(portfolio_id: str, conn: Conn, status: str | None = None,
               automation_id: Annotated[str | None, Query(alias="automationId")] = None,
               open_only: Annotated[bool, Query(alias="openOnly")] = False,
               limit: Annotated[int, Query(ge=1, le=1000)] = 100):
        load(conn, portfolio_id)
        return {"orders": orders_store.list_orders(conn, portfolio_id, automation_id, status, open_only, limit)}

    @r.get("/{portfolio_id}/fills")
    def fills(portfolio_id: str, conn: Conn, limit: Annotated[int, Query(ge=1, le=1000)] = 100):
        load(conn, portfolio_id)
        return {"fills": orders_store.list_fills(conn, portfolio_id, limit=limit)}

    @r.get("/{portfolio_id}/lots")
    def lots(portfolio_id: str, conn: Conn, open_only: Annotated[bool, Query(alias="openOnly")] = True):
        load(conn, portfolio_id)
        sql = "SELECT * FROM position_lots WHERE portfolio_id = ?" + (" AND closed_at IS NULL" if open_only else "")
        return {"lots": [store.lot_json(x) for x in conn.execute(sql + " ORDER BY opened_at, rowid", (portfolio_id,))]}

    @r.post("/{portfolio_id}/orders", status_code=201)
    def place_manual_order(portfolio_id: str, body: ManualOrderIn, conn: Conn):
        """Place an order by hand (paper account). Only halal symbols. With automationId, a buy uses that
        automation's budget and a sell uses its shares; without it, unallocated cash and manual shares."""
        p = load(conn, portfolio_id)
        symbol = body.symbol.upper()
        try:
            if symbol not in ctx.market.universe():
                raise HTTPException(422, f"{symbol} is not in the halal universe")
            snap = ctx.market.snapshots({symbol}).get(symbol, {})
        except MarketError as e:
            raise HTTPException(502, e.detail)
        if body.automationId:
            pool = budget.get_pool(conn, body.automationId)
            if not pool or pool["portfolioId"] != portfolio_id:
                raise HTTPException(422, "that automation has no budget in this portfolio")
        try:
            return service.place_order(
                conn, broker_for(conn, p), p, symbol=symbol, side=body.side, type=body.type, quantity=body.quantity,
                time_in_force=body.timeInForce, limit_price=body.limitPrice, stop_price=body.stopPrice,
                trail_percent=body.trailPercent, extended_hours=body.extendedHours, automation_id=body.automationId,
                estimated_price=Decimal(str(snap["latestPrice"])) if snap.get("latestPrice") is not None else None)
        except service.OrderRejected as e:
            raise HTTPException(422, f"{e.reason}: {e}")

    return r
