"""/v1/orders/{id}: one order with its fills, and cancelling."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from ..brokers import store as brokers_store
from ..brokers.base import BrokerError
from ..deps import AppContext
from . import service, store


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter(prefix="/v1/orders", tags=["orders"], dependencies=[Depends(ctx.require_key)])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]

    def load(conn, order_id: str) -> dict:
        o = store.get_order(conn, order_id)
        if not o:
            raise HTTPException(404, f"order '{order_id}' not found")
        return o

    @r.get("/{order_id}")
    def get_order(order_id: str, conn: Conn):
        o = load(conn, order_id)
        return {**o, "fills": store.list_fills(conn, o["portfolioId"], order_id=order_id)}

    @r.post("/{order_id}/cancel")
    def cancel(order_id: str, conn: Conn):
        o = load(conn, order_id)
        try:
            broker = ctx.broker_factory(brokers_store.get_account(conn, o["brokerAccountId"]))
            return service.cancel_order(conn, broker, order_id)
        except service.OrderRejected as e:
            raise HTTPException(409, str(e))
        except BrokerError as e:
            raise HTTPException(502, str(e))

    return r
