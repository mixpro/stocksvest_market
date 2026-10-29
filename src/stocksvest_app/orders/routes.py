"""/v1/orders/{id}: one order with its fills, and cancelling. /v1/orders/kill-all: the emergency stop."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..brokers import store as brokers_store
from ..brokers.base import BrokerError
from ..deps import AppContext
from . import kill_all as kill, service, store


class KillAllIn(BaseModel):
    """Body of POST /v1/orders/kill-all.

    Attributes:
        confirm: must be exactly "SELL ALL".

    Used by: router.kill_all.
    """
    confirm: str


def router(ctx: AppContext) -> APIRouter:
    """The /v1/orders routes.

    Args:
        ctx: app context (DB, broker factory, key check).

    Used by: api.create_app.
    """
    r = APIRouter(prefix="/v1/orders", tags=["orders"], dependencies=[Depends(ctx.require_key)])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]

    def load(conn, order_id: str) -> dict:
        """A stored order, or 404.

        Args:
            conn: app DB connection.
            order_id: the order's id.
        """
        o = store.get_order(conn, order_id)
        if not o:
            raise HTTPException(404, f"order '{order_id}' not found")
        return o

    @r.post("/kill-all")
    def kill_all(body: KillAllIn, conn: Conn):
        """Emergency stop: switch all scheduled automation runs off, cancel open buys, and sell every share held in
        every portfolio (paper and live) at market. Outside market hours the sells wait for the open. 422 unless
        `confirm` is "SELL ALL".
        \f
        Args:
            body: the confirmation.
            conn: app DB connection (injected).
        """
        if body.confirm != "SELL ALL":
            raise HTTPException(422, 'type "SELL ALL" to confirm')
        out = kill.kill_all(ctx, conn)
        return {"cancelled": out["cancelled"], "errors": out["errors"],
                "sells": [{k: o[k] for k in ("id", "portfolioId", "budgetId", "symbol", "quantity", "status")}
                          for o in out["sells"]]}

    @r.get("/{order_id}")
    def get_order(order_id: str, conn: Conn):
        """One order with its fills.
        \f
        Args:
            order_id: the order's id (path).
            conn: app DB connection (injected).
        """
        o = load(conn, order_id)
        return {**o, "fills": store.list_fills(conn, o["portfolioId"], order_id=order_id)}

    @r.post("/{order_id}/cancel")
    def cancel(order_id: str, conn: Conn):
        """Cancel an open order at the broker. 409 when it is already final or not at the broker yet; 502 when the broker fails.
        \f
        Args:
            order_id: the order's id (path).
            conn: app DB connection (injected).
        """
        o = load(conn, order_id)
        try:
            broker = ctx.broker_factory(brokers_store.get_account(conn, o["brokerAccountId"]))
            return service.cancel_order(conn, broker, order_id)
        except service.OrderRejected as e:
            raise HTTPException(409, str(e))
        except BrokerError as e:
            raise HTTPException(502, str(e))

    return r
