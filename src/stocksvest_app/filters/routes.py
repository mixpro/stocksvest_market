"""/v1/filters, /v1/filter-groups and /v1/filter-actions."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from ..automations import store as automations_store
from ..deps import AppContext
from ..market.client import MarketError
from ..portfolios import store as portfolios_store
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
    """The values of an enum, for editor metadata.

    Args:
        enum: an Enum class.

    Used by: router.filter_actions.
    """
    return [e.value for e in enum]


def _http(e: EngineError | MarketError) -> HTTPException:
    """Turn an engine or market failure into an HTTP error.

    Args:
        e: EngineError (-> 422) or MarketError (its status, 5xx -> 502).

    Used by: the filter and group routes.
    """
    if isinstance(e, MarketError):
        return HTTPException(e.status if e.status < 500 else 502, e.detail)
    return HTTPException(422, str(e))


def router(ctx: AppContext) -> APIRouter:
    """The /v1/filters, /v1/filter-groups and /v1/filter-actions routes.

    Args:
        ctx: app context (DB, market helpers, key check).

    Used by: api.create_app.
    """
    r = APIRouter(dependencies=[Depends(ctx.require_key)])
    Conn = Annotated[sqlite3.Connection, Depends(ctx.conn)]

    def evaluator(conn) -> Evaluator:
        """A fresh Evaluator for one request.

        Args:
            conn: app DB connection.
        """
        return Evaluator(conn, ctx.market)

    # ---------------------------------------------------------------- editor metadata

    @r.get("/v1/filter-actions", tags=["filters"])
    def filter_actions(conn: Conn):
        """Everything a client needs to build the filter editor: input types and action fields.
        \f
        Sector options come from the market API (empty, and SECTOR marked unavailable, when it is down).
        Portfolio options are the app's portfolios (POSITION is unavailable until one exists).

        Args:
            conn: app DB connection (injected).
        """
        try:
            sectors = ctx.market.sector_options()
        except MarketError:
            sectors = []
        portfolios = [{"value": p["id"], "label": p["name"]} for p in portfolios_store.list_portfolios(conn)]
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
                {"type": "POSITION", "label": "In portfolio positions", "available": bool(portfolios), "fields": [
                    {"name": "portfolioIds", "kind": "multiselect", "options": portfolios, "required": True}]},
            ],
        }

    # ---------------------------------------------------------------- filters

    def load_filter(conn, filter_id: str) -> dict:
        """A stored filter, or 404.

        Args:
            conn: app DB connection.
            filter_id: the filter's id.
        """
        f = store.get_filter(conn, filter_id)
        if not f:
            raise HTTPException(404, f"filter '{filter_id}' not found")
        return f

    def check_filter(conn, body: FilterIn, filter_id: str | None) -> None:
        """check_filter_refs, as 422 on failure.

        Args:
            conn: app DB connection.
            body: the filter being saved.
            filter_id: its id when updating, else None.
        """
        try:
            check_filter_refs(conn, body, filter_id)
        except EngineError as e:
            raise _http(e)

    @r.get("/v1/filters", tags=["filters"])
    def list_filters(conn: Conn):
        """Every filter, by name.
        \f
        Args:
            conn: app DB connection (injected).
        """
        return {"filters": store.list_filters(conn)}

    @r.post("/v1/filters", tags=["filters"], status_code=201)
    def create_filter(body: FilterIn, conn: Conn):
        """Create a filter. Missing references are refused (422).
        \f
        Args:
            body: the filter.
            conn: app DB connection (injected).
        """
        check_filter(conn, body, None)
        return store.save_filter(conn, body.model_dump(mode="json"))

    @r.post("/v1/filters/preview", tags=["filters"])
    def preview_filter(body: FilterIn, conn: Conn):
        """Run an unsaved filter. Nothing is stored.
        \f
        Args:
            body: the filter to try.
            conn: app DB connection (injected).
        """
        check_filter(conn, body, None)
        ev = evaluator(conn)
        try:
            out = ev.evaluate(body)
            return {"steps": out["steps"], "symbols": ev.describe(out["symbols"])}
        except (EngineError, MarketError) as e:
            raise _http(e)

    @r.get("/v1/filters/{filter_id}", tags=["filters"])
    def get_filter(filter_id: str, conn: Conn):
        """One filter.
        \f
        Args:
            filter_id: the filter's id.
            conn: app DB connection (injected).
        """
        return load_filter(conn, filter_id)

    @r.put("/v1/filters/{filter_id}", tags=["filters"])
    def update_filter(filter_id: str, body: FilterIn, conn: Conn):
        """Replace a filter. Missing references and cycles are refused (422).
        \f
        Args:
            filter_id: the filter's id.
            body: the new definition.
            conn: app DB connection (injected).
        """
        load_filter(conn, filter_id)
        check_filter(conn, body, filter_id)
        return store.save_filter(conn, body.model_dump(mode="json"), filter_id)

    @r.delete("/v1/filters/{filter_id}", tags=["filters"], status_code=204)
    def delete_filter(filter_id: str, conn: Conn):
        """Delete a filter. Refused (409) while another filter, a group or an automation uses it.
        \f
        Args:
            filter_id: the filter's id.
            conn: app DB connection (injected).
        """
        load_filter(conn, filter_id)
        users = store.filter_users(conn, filter_id) + automations_store.automations_using(conn, "FILTER", filter_id)
        if users:
            raise HTTPException(409, f"filter is used by: {', '.join(users)}")
        store.delete_filter(conn, filter_id)

    @r.post("/v1/filters/{filter_id}/run", tags=["filters"])
    def run_filter(filter_id: str, conn: Conn):
        """Run a saved filter and store the run (with prices at run time) in the history.
        \f
        Args:
            filter_id: the filter's id.
            conn: app DB connection (injected).
        """
        stored = load_filter(conn, filter_id)
        f = FilterIn.model_validate(stored)

        def go():
            """The filter run itself (result for the run history)."""
            ev = evaluator(conn)
            try:
                out = ev.evaluate(f, (filter_id,))
                return {"steps": out["steps"], "symbols": ev.describe(out["symbols"])}
            except (EngineError, MarketError) as e:
                raise _http(e)

        return record(ctx, conn, "FILTER", filter_id, stored, go)

    @r.get("/v1/filters/{filter_id}/runs", tags=["filters"])
    def filter_runs(filter_id: str, conn: Conn, limit: Annotated[int, Query(ge=1, le=500)] = 20):
        """A filter's run history, newest first. Also works for deleted filters: their run history is kept.
        \f
        Args:
            filter_id: the filter's id.
            conn: app DB connection (injected).
            limit: maximum runs.
        """
        runs = runs_store.list_runs(conn, "FILTER", filter_id, limit=limit)
        if not runs and not store.get_filter(conn, filter_id):
            raise HTTPException(404, f"filter '{filter_id}' not found")
        return {"runs": runs}

    # ---------------------------------------------------------------- groups

    def load_group(conn, group_id: str) -> dict:
        """A stored group, or 404.

        Args:
            conn: app DB connection.
            group_id: the group's id.
        """
        g = store.get_group(conn, group_id)
        if not g:
            raise HTTPException(404, f"filter group '{group_id}' not found")
        return g

    def check_group(conn, filter_ids: list[str], group_id: str | None) -> None:
        """check_group_refs, as 422 on failure.

        Args:
            conn: app DB connection.
            filter_ids: the members being saved.
            group_id: the group's id when updating, else None.
        """
        try:
            check_group_refs(conn, filter_ids, group_id)
        except EngineError as e:
            raise _http(e)

    @r.get("/v1/filter-groups", tags=["filter groups"])
    def list_groups(conn: Conn):
        """Every filter group, by name.
        \f
        Args:
            conn: app DB connection (injected).
        """
        return {"groups": store.list_groups(conn)}

    @r.post("/v1/filter-groups", tags=["filter groups"], status_code=201)
    def create_group(body: GroupIn, conn: Conn):
        """Create a filter group.
        \f
        Args:
            body: the group.
            conn: app DB connection (injected).
        """
        check_group(conn, body.filterIds, None)
        return store.save_group(conn, body.model_dump())

    @r.get("/v1/filter-groups/{group_id}", tags=["filter groups"])
    def get_group(group_id: str, conn: Conn):
        """One filter group.
        \f
        Args:
            group_id: the group's id.
            conn: app DB connection (injected).
        """
        return load_group(conn, group_id)

    @r.put("/v1/filter-groups/{group_id}", tags=["filter groups"])
    def update_group(group_id: str, body: GroupIn, conn: Conn):
        """Replace a group and its members. Missing filters, duplicates and cycles are refused (422).
        \f
        Args:
            group_id: the group's id.
            body: the new definition.
            conn: app DB connection (injected).
        """
        load_group(conn, group_id)
        check_group(conn, body.filterIds, group_id)
        return store.save_group(conn, body.model_dump(), group_id)

    @r.delete("/v1/filter-groups/{group_id}", tags=["filter groups"], status_code=204)
    def delete_group(group_id: str, conn: Conn):
        """Delete a group. Refused (409) while a filter or an automation uses it.
        \f
        Args:
            group_id: the group's id.
            conn: app DB connection (injected).
        """
        load_group(conn, group_id)
        users = store.group_users(conn, group_id) + automations_store.automations_using(conn, "FILTER_GROUP", group_id)
        if users:
            raise HTTPException(409, f"group is used by: {', '.join(users)}")
        store.delete_group(conn, group_id)

    @r.post("/v1/filter-groups/{group_id}/run", tags=["filter groups"])
    def run_group(group_id: str, conn: Conn):
        """Run every filter in the group. `symbols` is the union of the member results.
        \f
        Args:
            group_id: the group's id.
            conn: app DB connection (injected).
        """
        group = load_group(conn, group_id)
        filters = [load_filter(conn, fid) for fid in group["filterIds"]]

        def go():
            """The group run itself (per-member steps and the union, for the run history)."""
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
        """A group's run history, newest first. Also works for deleted groups: their run history is kept.
        \f
        Args:
            group_id: the group's id.
            conn: app DB connection (injected).
            limit: maximum runs.
        """
        runs = runs_store.list_runs(conn, "FILTER_GROUP", group_id, limit=limit)
        if not runs and not store.get_group(conn, group_id):
            raise HTTPException(404, f"filter group '{group_id}' not found")
        return {"runs": runs}

    return r
