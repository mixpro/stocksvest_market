"""/v1/filters, /v1/filter-groups and /v1/filter-actions."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import AppContext
from ..market.client import MarketError
from ..runs import store as runs_store
from ..runs.routes import record
from . import store
from .engine import EngineError, Evaluator, check_filter_refs, check_group_refs
from .models import (
    AVAILABLE_INPUTS,
    AssetType,
    Condition,
    FilterIn,
    GroupIn,
    InputType,
    PriceReference,
    TimePeriodUnit,
    ValueType,
)


def _values(enum) -> list[str]:
    return [e.value for e in enum]


def _http(e: EngineError | MarketError) -> HTTPException:
    if isinstance(e, MarketError):
        return HTTPException(e.status if e.status < 500 else 502, e.detail)
    return HTTPException(422, str(e))


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter(dependencies=[Depends(ctx.require_key)])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]

    def evaluator(conn) -> Evaluator:
        return Evaluator(conn, ctx.market)

    # ---------------------------------------------------------------- editor metadata

    @r.get("/v1/filter-actions", tags=["filters"])
    def filter_actions():
        """Everything a client needs to build the filter editor: input types and action fields."""
        try:
            sectors = ctx.market.sector_options()
        except MarketError:
            sectors = []
        condition = {"name": "condition", "kind": "select", "options": _values(Condition), "required": True}
        value = {"name": "value", "kind": "number", "required": True}
        max_value = {"name": "maxValue", "kind": "number", "requiredWhen": {"condition": "BETWEEN"}}
        return {
            "inputTypes": [
                {"type": t.value, "needsId": t != InputType.UNIVERSE, "available": t in AVAILABLE_INPUTS}
                for t in InputType
            ],
            "actions": [
                {"type": "SYMBOL_TYPE", "label": "Symbol type", "available": True, "fields": [
                    {"name": "assetTypes", "kind": "multiselect", "options": _values(AssetType), "required": True}]},
                {"type": "SECTOR", "label": "Sector", "available": bool(sectors), "fields": [
                    {"name": "sectorIds", "kind": "multiselect", "options": sectors, "required": True}]},
                {"type": "SYMBOL_PRICE", "label": "Price", "available": True, "fields": [
                    {"name": "priceReference", "kind": "select", "options": _values(PriceReference), "required": True},
                    condition, value, max_value]},
                {"type": "PRICE_CHANGE", "label": "Price change", "available": True, "fields": [
                    {"name": "periodAmount", "kind": "integer", "required": True, "min": 1},
                    {"name": "periodUnit", "kind": "select", "options": _values(TimePeriodUnit), "required": True},
                    {"name": "valueType", "kind": "select", "options": _values(ValueType), "required": True},
                    condition, value, max_value]},
            ],
        }

    # ---------------------------------------------------------------- filters

    def load_filter(conn, filter_id: str) -> dict:
        f = store.get_filter(conn, filter_id)
        if not f:
            raise HTTPException(404, f"filter '{filter_id}' not found")
        return f

    def check_filter(conn, body: FilterIn, filter_id: str | None) -> None:
        try:
            check_filter_refs(conn, body, filter_id)
        except EngineError as e:
            raise _http(e)

    @r.get("/v1/filters", tags=["filters"])
    def list_filters(conn: Conn):
        return {"filters": store.list_filters(conn)}

    @r.post("/v1/filters", tags=["filters"], status_code=201)
    def create_filter(body: FilterIn, conn: Conn):
        check_filter(conn, body, None)
        return store.save_filter(conn, body.model_dump(mode="json"))

    @r.post("/v1/filters/preview", tags=["filters"])
    def preview_filter(body: FilterIn, conn: Conn):
        """Run an unsaved filter. Nothing is stored."""
        check_filter(conn, body, None)
        ev = evaluator(conn)
        try:
            out = ev.evaluate(body)
            return {"steps": out["steps"], "symbols": ev.describe(out["symbols"])}
        except (EngineError, MarketError) as e:
            raise _http(e)

    @r.get("/v1/filters/{filter_id}", tags=["filters"])
    def get_filter(filter_id: str, conn: Conn):
        return load_filter(conn, filter_id)

    @r.put("/v1/filters/{filter_id}", tags=["filters"])
    def update_filter(filter_id: str, body: FilterIn, conn: Conn):
        load_filter(conn, filter_id)
        check_filter(conn, body, filter_id)
        return store.save_filter(conn, body.model_dump(mode="json"), filter_id)

    @r.delete("/v1/filters/{filter_id}", tags=["filters"], status_code=204)
    def delete_filter(filter_id: str, conn: Conn):
        load_filter(conn, filter_id)
        users = store.filter_users(conn, filter_id)
        if users:
            raise HTTPException(409, f"filter is used by: {', '.join(users)}")
        store.delete_filter(conn, filter_id)

    @r.post("/v1/filters/{filter_id}/run", tags=["filters"])
    def run_filter(filter_id: str, conn: Conn):
        """Run a saved filter and store the run (with prices at run time) in the history."""
        stored = load_filter(conn, filter_id)
        f = FilterIn.model_validate(stored)

        def go():
            ev = evaluator(conn)
            try:
                out = ev.evaluate(f, (filter_id,))
                return {"steps": out["steps"], "symbols": ev.describe(out["symbols"])}
            except (EngineError, MarketError) as e:
                raise _http(e)

        return record(ctx, conn, "FILTER", filter_id, stored, go)

    @r.get("/v1/filters/{filter_id}/runs", tags=["filters"])
    def filter_runs(filter_id: str, conn: Conn, limit: Annotated[int, Query(ge=1, le=500)] = 20):
        return {"runs": runs_store.list_runs(conn, "FILTER", filter_id, limit=limit)}

    # ---------------------------------------------------------------- groups

    def load_group(conn, group_id: str) -> dict:
        g = store.get_group(conn, group_id)
        if not g:
            raise HTTPException(404, f"filter group '{group_id}' not found")
        return g

    def check_group(conn, filter_ids: list[str], group_id: str | None) -> None:
        try:
            check_group_refs(conn, filter_ids, group_id)
        except EngineError as e:
            raise _http(e)

    @r.get("/v1/filter-groups", tags=["filter groups"])
    def list_groups(conn: Conn):
        return {"groups": store.list_groups(conn)}

    @r.post("/v1/filter-groups", tags=["filter groups"], status_code=201)
    def create_group(body: GroupIn, conn: Conn):
        check_group(conn, body.filterIds, None)
        return store.save_group(conn, body.model_dump())

    @r.get("/v1/filter-groups/{group_id}", tags=["filter groups"])
    def get_group(group_id: str, conn: Conn):
        return load_group(conn, group_id)

    @r.put("/v1/filter-groups/{group_id}", tags=["filter groups"])
    def update_group(group_id: str, body: GroupIn, conn: Conn):
        load_group(conn, group_id)
        check_group(conn, body.filterIds, group_id)
        return store.save_group(conn, body.model_dump(), group_id)

    @r.delete("/v1/filter-groups/{group_id}", tags=["filter groups"], status_code=204)
    def delete_group(group_id: str, conn: Conn):
        load_group(conn, group_id)
        users = store.group_users(conn, group_id)
        if users:
            raise HTTPException(409, f"group is used by: {', '.join(users)}")
        store.delete_group(conn, group_id)

    @r.post("/v1/filter-groups/{group_id}/run", tags=["filter groups"])
    def run_group(group_id: str, conn: Conn):
        """Run every filter in the group. `symbols` is the union of the member results."""
        group = load_group(conn, group_id)
        filters = [load_filter(conn, fid) for fid in group["filterIds"]]

        def go():
            ev = evaluator(conn)
            members, union = [], set()
            try:
                for f in filters:
                    out = ev.evaluate(FilterIn.model_validate(f), (f["id"],))
                    members.append({"filterId": f["id"], "name": f["name"], "steps": out["steps"],
                                    "count": len(out["symbols"])})
                    union |= out["symbols"]
                return {"filters": members, "symbols": ev.describe(union)}
            except (EngineError, MarketError) as e:
                raise _http(e)

        return record(ctx, conn, "FILTER_GROUP", group_id, {"group": group, "filters": filters}, go)

    @r.get("/v1/filter-groups/{group_id}/runs", tags=["filter groups"])
    def group_runs(group_id: str, conn: Conn, limit: Annotated[int, Query(ge=1, le=500)] = 20):
        return {"runs": runs_store.list_runs(conn, "FILTER_GROUP", group_id, limit=limit)}

    return r
