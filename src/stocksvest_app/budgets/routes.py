"""/v1/portfolios/{id}/budgets and /v1/budgets: a portfolio's budgets, their money and their automations."""

import sqlite3
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..automations import store as automations_store
from ..deps import AppContext
from ..orders import store as orders_store
from ..portfolios import store as portfolios_store
from ..portfolios.store import num
from . import store


class BudgetIn(BaseModel):
    """Body of POST /v1/portfolios/{id}/budgets.

    Attributes:
        name: unique within the portfolio, 1-100 characters.
        amount: starting money, taken from the portfolio's unallocated cash (default 0).
        automationIds: automations that will trade with it.

    Used by: router.create_budget.
    """

    name: str = Field(min_length=1, max_length=100)
    amount: Decimal = Field(default=Decimal(0), ge=0, decimal_places=2)
    automationIds: list[str] = []


class BudgetUpdate(BaseModel):
    """Body of PUT /v1/budgets/{id}.

    Attributes:
        name: the new name, unique within the portfolio.

    Used by: router.update_budget.
    """

    name: str = Field(min_length=1, max_length=100)


class BudgetChangeIn(BaseModel):
    """Body of budget allocate / withdraw.

    Attributes:
        amount: > 0, cents.
        note: free text for the ledger.

    Used by: router.allocate, router.withdraw.
    """

    amount: Decimal = Field(gt=0, decimal_places=2)
    note: str | None = None


class BudgetAutomationIn(BaseModel):
    """Body of POST /v1/budgets/{id}/automations.

    Attributes:
        automationId: the automation to attach.

    Used by: router.add_automation.
    """

    automationId: str


def router(ctx: AppContext) -> APIRouter:
    """The budget routes.

    Args:
        ctx: app context (DB, key check).

    Used by: api.create_app.
    """
    r = APIRouter(tags=["budgets"], dependencies=[Depends(ctx.require_key)])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]

    def portfolio_or_404(conn, portfolio_id: str) -> dict:
        """A stored portfolio, or 404.

        Args:
            conn: app DB connection.
            portfolio_id: the portfolio's id.
        """
        p = portfolios_store.get_portfolio(conn, portfolio_id)
        if not p:
            raise HTTPException(404, f"portfolio '{portfolio_id}' not found")
        return p

    def load(conn, budget_id: str) -> dict:
        """A stored budget, or 404.

        Args:
            conn: app DB connection.
            budget_id: the budget's id.
        """
        b = store.get_budget(conn, budget_id)
        if not b:
            raise HTTPException(404, f"budget '{budget_id}' not found")
        return b

    def automation_or_422(conn, automation_id: str) -> dict:
        """A stored automation, or 422.

        Args:
            conn: app DB connection.
            automation_id: the automation's id.
        """
        a = automations_store.get_automation(conn, automation_id)
        if not a:
            raise HTTPException(422, f"automation '{automation_id}' does not exist")
        return a

    def attach(conn, budget: dict, automation: dict) -> None:
        """Attach an automation and note it in the automation's change log.

        Args:
            conn: app DB connection.
            budget: the budget.
            automation: the automation.
        """
        if store.add_automation(conn, budget["id"], automation["id"]):
            with conn:
                automations_store.add_event(conn, automation["id"], "BUDGET_ADDED", automation["configVersion"],
                                            {"budgetId": budget["id"], "budget": budget["name"],
                                             "portfolioId": budget["portfolioId"]})

    def detail(conn, budget: dict) -> dict:
        """A budget with its shares (by buying automation) and open orders.

        Args:
            conn: app DB connection.
            budget: the budget.
        """
        positions = portfolios_store.positions(conn, budget["portfolioId"], budget_id=budget["id"], by_owner=True)
        return {**budget,
                "positions": [{"symbol": x["symbol"], "automationId": x["automationId"], "quantity": num(x["quantity"]),
                               "averageCost": num(x["averageCost"]), "costBasis": num(x["costBasis"])}
                              for x in positions],
                "openOrders": orders_store.list_orders(conn, budget["portfolioId"], budget_id=budget["id"],
                                                       open_only=True)}

    @r.get("/v1/portfolios/{portfolio_id}/budgets")
    def list_budgets(portfolio_id: str, conn: Conn):
        """A portfolio's budgets, by name, each with its automations.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            conn: app DB connection (injected).
        """
        portfolio_or_404(conn, portfolio_id)
        return {"budgets": store.list_budgets(conn, portfolio_id)}

    @r.post("/v1/portfolios/{portfolio_id}/budgets", status_code=201)
    def create_budget(portfolio_id: str, body: BudgetIn, conn: Conn):
        """Create a budget in the portfolio. `amount` comes from the portfolio's unallocated cash; the automations
        in `automationIds` trade with it from their next run.
        \f
        Args:
            portfolio_id: the portfolio's id (path).
            body: name, starting money and automations.
            conn: app DB connection (injected).
        """
        p = portfolio_or_404(conn, portfolio_id)
        automations = [automation_or_422(conn, aid) for aid in dict.fromkeys(body.automationIds)]
        try:
            b = store.create_budget(conn, p, body.name, body.amount)
        except store.BudgetError as e:
            raise HTTPException(409 if "named" in str(e) else 422, str(e))
        for a in automations:
            attach(conn, b, a)
        return detail(conn, store.get_budget(conn, b["id"]))

    @r.get("/v1/budgets/{budget_id}")
    def get_budget(budget_id: str, conn: Conn):
        """The budget's money, its automations, its shares (with who bought them) and its open orders.
        \f
        Args:
            budget_id: the budget's id (path).
            conn: app DB connection (injected).
        """
        return detail(conn, load(conn, budget_id))

    @r.put("/v1/budgets/{budget_id}")
    def update_budget(budget_id: str, body: BudgetUpdate, conn: Conn):
        """Rename the budget.
        \f
        Args:
            budget_id: the budget's id (path).
            body: the new name.
            conn: app DB connection (injected).
        """
        load(conn, budget_id)
        try:
            return store.rename_budget(conn, budget_id, body.name)
        except store.BudgetError as e:
            raise HTTPException(409, str(e))

    @r.delete("/v1/budgets/{budget_id}", status_code=204)
    def delete_budget(budget_id: str, conn: Conn):
        """Delete the budget: its cash goes back to the portfolio and its automations are detached. Refused (409)
        while it holds shares or has open orders. Its ledger and history are kept.
        \f
        Args:
            budget_id: the budget's id (path).
            conn: app DB connection (injected).
        """
        b = load(conn, budget_id)
        try:
            store.delete_budget(conn, budget_id)
        except store.BudgetError as e:
            raise HTTPException(409, str(e))
        for a in b["automations"]:
            full = automations_store.get_automation(conn, a["id"])
            with conn:
                automations_store.add_event(conn, a["id"], "BUDGET_REMOVED", full["configVersion"],
                                            {"budgetId": budget_id, "budget": b["name"], "reason": "budget deleted"})

    @r.post("/v1/budgets/{budget_id}/allocate")
    def allocate(budget_id: str, body: BudgetChangeIn, conn: Conn):
        """Move money from the portfolio's unallocated cash into the budget.
        \f
        Args:
            budget_id: the budget's id (path).
            body: amount and an optional note.
            conn: app DB connection (injected).
        """
        b = load(conn, budget_id)
        try:
            store.allocate(conn, budget_id, portfolios_store.get_portfolio(conn, b["portfolioId"]), body.amount,
                           body.note)
        except store.BudgetError as e:
            raise HTTPException(422, str(e))
        return detail(conn, load(conn, budget_id))

    @r.post("/v1/budgets/{budget_id}/withdraw")
    def withdraw(budget_id: str, body: BudgetChangeIn, conn: Conn):
        """Give available money (not reserved, not invested) back to the portfolio's unallocated cash.
        \f
        Args:
            budget_id: the budget's id (path).
            body: amount and an optional note.
            conn: app DB connection (injected).
        """
        load(conn, budget_id)
        try:
            store.withdraw(conn, budget_id, body.amount, body.note)
        except store.BudgetError as e:
            raise HTTPException(422, str(e))
        return detail(conn, load(conn, budget_id))

    @r.get("/v1/budgets/{budget_id}/ledger")
    def ledger(budget_id: str, conn: Conn, limit: Annotated[int, Query(ge=1, le=1000)] = 200):
        """Every change of the budget's money, newest first, with the automation that caused it. Also works for
        deleted budgets.
        \f
        Args:
            budget_id: the budget's id (path).
            conn: app DB connection (injected).
            limit: how many to return, newest first (query).
        """
        entries = store.entries(conn, budget_id, limit)
        if not entries and not store.get_budget(conn, budget_id):
            raise HTTPException(404, f"budget '{budget_id}' not found")
        return {"entries": entries}

    @r.post("/v1/budgets/{budget_id}/automations")
    def add_automation(budget_id: str, body: BudgetAutomationIn, conn: Conn):
        """Let an automation trade with this budget (from its next run). One automation can be in several
        budgets, also of other portfolios.
        \f
        Args:
            budget_id: the budget's id (path).
            body: the automation.
            conn: app DB connection (injected).
        """
        b = load(conn, budget_id)
        attach(conn, b, automation_or_422(conn, body.automationId))
        return load(conn, budget_id)

    @r.delete("/v1/budgets/{budget_id}/automations/{automation_id}")
    def remove_automation(budget_id: str, automation_id: str, conn: Conn):
        """Stop an automation trading with this budget. Shares it bought stay in the budget, and its open orders
        settle normally.
        \f
        Args:
            budget_id: the budget's id (path).
            automation_id: the automation's id (path).
            conn: app DB connection (injected).
        """
        b = load(conn, budget_id)
        if not store.remove_automation(conn, budget_id, automation_id):
            raise HTTPException(404, f"automation '{automation_id}' is not in this budget")
        a = automations_store.get_automation(conn, automation_id)
        if a:
            with conn:
                automations_store.add_event(conn, automation_id, "BUDGET_REMOVED", a["configVersion"],
                                            {"budgetId": budget_id, "budget": b["name"]})
        return load(conn, budget_id)

    return r
