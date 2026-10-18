"""Evaluate filters: resolve inputs (union), then narrow the symbols with each action in order."""

import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..market.data import MarketData
from . import store
from .models import (
    AVAILABLE_INPUTS,
    FilterAction,
    FilterIn,
    InputType,
    PriceChangeAction,
    PriceReference,
    SectorAction,
    SymbolPriceAction,
    SymbolTypeAction,
    ValueType,
)

ET = ZoneInfo("America/New_York")  # Used by: Evaluator._last_close (today's date in New York)


class EngineError(ValueError):
    """A filter that cannot run as defined (missing reference, cycle, unsupported input).

    Used by: check_filter_refs / check_group_refs / Evaluator (raised); filters.routes (-> 422),
    automations.runner (-> FAILED run).
    """


# ------------------------------------------------------------------ reference checks


def find_cycle(conn: sqlite3.Connection, start: tuple[str, str], start_deps: list[tuple[str, str]]) -> bool:
    """True if `start` (("F", id) or ("G", id)) can reach itself through `start_deps` and stored references.

    Args:
        conn: app DB connection (stored filters and groups).
        start: the node being saved: ("F", filter id) or ("G", group id).
        start_deps: what it would reference after the save (instead of what is stored).

    Used by: check_filter_refs, check_group_refs.
    """

    def deps(node):
        """What a node references: a filter's FILTER/FILTER_GROUP inputs, or a group's members.

        Args:
            node: ("F", id) or ("G", id).
        """
        kind, nid = node
        if node == start:
            return start_deps
        if kind == "F":
            f = store.get_filter(conn, nid)
            return [("F" if i["type"] == "FILTER" else "G", i["id"]) for i in (f["inputs"] if f else [])
                    if i["type"] in ("FILTER", "FILTER_GROUP")]
        g = store.get_group(conn, nid)
        return [("F", fid) for fid in (g["filterIds"] if g else [])]

    seen, stack = set(), list(start_deps)
    while stack:
        node = stack.pop()
        if node == start:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(deps(node))
    return False


def check_filter_refs(conn, f: FilterIn, filter_id: str | None) -> None:
    """Refuse a filter whose inputs reference missing filters/groups or would create a cycle.

    Args:
        conn: app DB connection.
        f: the filter being saved or previewed.
        filter_id: its id when updating (cycles are only possible for existing filters); None for new/preview.

    Raises:
        EngineError: with a readable reason.

    Used by: routes (create, update, preview).
    """
    deps = []
    for inp in f.inputs:
        if inp.type == InputType.FILTER:
            if not store.get_filter(conn, inp.id):
                raise EngineError(f"input filter '{inp.id}' does not exist")
            deps.append(("F", inp.id))
        elif inp.type == InputType.FILTER_GROUP:
            if not store.get_group(conn, inp.id):
                raise EngineError(f"input filter group '{inp.id}' does not exist")
            deps.append(("G", inp.id))
    if filter_id and find_cycle(conn, ("F", filter_id), deps):
        raise EngineError("inputs would create a cycle (this filter would end up using itself)")


def check_group_refs(conn, filter_ids: list[str], group_id: str | None) -> None:
    """Refuse group members that don't exist, repeat, or would create a cycle.

    Args:
        conn: app DB connection.
        filter_ids: the members being saved.
        group_id: the group's id when updating; None for a new group.

    Raises:
        EngineError: with a readable reason.

    Used by: routes (create, update group).
    """
    for fid in filter_ids:
        if not store.get_filter(conn, fid):
            raise EngineError(f"filter '{fid}' does not exist")
    if len(set(filter_ids)) != len(filter_ids):
        raise EngineError("a filter is listed twice")
    if group_id and find_cycle(conn, ("G", group_id), [("F", fid) for fid in filter_ids]):
        raise EngineError("members would create a cycle (a member filter uses this group as input)")


# ------------------------------------------------------------------ evaluation


class Evaluator:
    """One evaluation. Referenced filters are re-run live, but each at most once per evaluation.
    Market snapshots are fetched once and reused by every action and the result rows.

    Used by: filters.routes (preview, run, group run), automations.runner (run targets and planner prices).
    """

    def __init__(self, conn: sqlite3.Connection, market: MarketData, now: datetime | None = None):
        """Args:
            conn: app DB connection (stored filters and groups).
            market: market helpers.
            now: evaluation time (tests pin it); default now.
        """
        self.conn = conn
        self.market = market
        self.now = now or datetime.now(timezone.utc)
        self._filter_results: dict[str, set[str]] = {}
        self._snapshots: dict[str, dict] = {}
        self._session_open: bool | None = None

    # -- inputs

    def _input_symbols(self, inputs, path: tuple[str, ...]) -> set[str]:
        """The union of a filter's inputs.

        Args:
            inputs: FilterInput list.
            path: filter ids being evaluated above this one (cycle guard).

        Raises:
            EngineError: unavailable input type, missing reference or cycle.
        """
        out: set[str] = set()
        for inp in inputs:
            if inp.type not in AVAILABLE_INPUTS:
                raise EngineError(f"{inp.type.value} inputs are not available yet")
            if inp.type == InputType.UNIVERSE:
                out |= set(self.market.universe())
            elif inp.type == InputType.FILTER:
                out |= self.filter_symbols(inp.id, path)
            else:
                out |= self.group_symbols(inp.id, path)
        return out

    def filter_symbols(self, filter_id: str, path: tuple[str, ...] = ()) -> set[str]:
        """A stored filter's result, evaluated live once per evaluation.

        Args:
            filter_id: the filter's id.
            path: filter ids being evaluated above this one.

        Raises:
            EngineError: missing filter or cycle.
        """
        if filter_id in path:
            raise EngineError("cycle between filters")
        if filter_id not in self._filter_results:
            stored = store.get_filter(self.conn, filter_id)
            if not stored:
                raise EngineError(f"filter '{filter_id}' does not exist")
            f = FilterIn.model_validate(stored)
            self._filter_results[filter_id] = self.evaluate(f, path + (filter_id,))["symbols"]
        return self._filter_results[filter_id]

    def group_symbols(self, group_id: str, path: tuple[str, ...] = ()) -> set[str]:
        """A group's result: the union of its member filters.

        Args:
            group_id: the group's id.
            path: filter ids being evaluated above this one.

        Raises:
            EngineError: missing group.
        """
        group = store.get_group(self.conn, group_id)
        if not group:
            raise EngineError(f"filter group '{group_id}' does not exist")
        out: set[str] = set()
        for fid in group["filterIds"]:
            out |= self.filter_symbols(fid, path)
        return out

    # -- actions

    def _snaps(self, tickers: set[str]) -> dict[str, dict]:
        """Market snapshots for these tickers, fetching only those not seen yet in this evaluation.

        Args:
            tickers: tickers needed.
        """
        missing = tickers - set(self._snapshots)
        if missing:
            self._snapshots.update(self.market.snapshots(missing))
        return {t: self._snapshots[t] for t in tickers if t in self._snapshots}

    def _last_close(self, snap: dict) -> float | None:
        """Close of the last completed trading day: during a trading day the latest daily bar is still forming.

        Args:
            snap: a market snapshot (dailyClose, dailyDate, previousClose).
        """
        today = self.now.astimezone(ET).date().isoformat()
        if self._session_open is None:
            day = self.market.trading_day(today)
            self._session_open = bool(day) and self.now < datetime.fromisoformat(day["regularClose"])
        if self._session_open and snap.get("dailyDate") == today:
            return snap.get("previousClose")
        return snap.get("dailyClose")

    def apply(self, action: FilterAction, symbols: set[str]) -> set[str]:
        """Keep the symbols that pass one action.

        Args:
            action: SymbolTypeAction, SectorAction, SymbolPriceAction or PriceChangeAction.
            symbols: the symbols still in the running.

        Returns:
            The subset that passes; symbols without the needed data are dropped.
        """
        if not symbols:
            return symbols
        if isinstance(action, SymbolTypeAction):
            allowed = {a.value for a in action.assetTypes}
            universe = self.market.universe()
            return {t for t in symbols if universe.get(t, {}).get("assetType") in allowed}
        if isinstance(action, SectorAction):
            allowed = set(action.sectorIds)
            universe = self.market.universe()
            return {t for t in symbols if universe.get(t, {}).get("sector") in allowed}
        if isinstance(action, SymbolPriceAction):
            snaps = self._snaps(symbols)
            if action.priceReference == PriceReference.LATEST_BAR:
                return {t for t, s in snaps.items() if action.matches(s.get("latestPrice"))}
            return {t for t, s in snaps.items() if action.matches(self._last_close(s))}
        if isinstance(action, PriceChangeAction):
            changes = self.market.price_changes(symbols, action.periodAmount, action.periodUnit.value)
            field = "changePercent" if action.valueType == ValueType.PERCENT else "change"
            return {t for t, c in changes.items() if action.matches(c.get(field))}
        raise EngineError(f"unknown action {action}")

    # -- entry points

    def evaluate(self, f: FilterIn, path: tuple[str, ...] = ()) -> dict:
        """Run a filter: inputs (union), then each action in order.

        Args:
            f: the filter.
            path: filter ids being evaluated above this one (callers pass (own id,) for stored filters).

        Returns:
            {"symbols": set of tickers, "steps": [{"step": "INPUTS" | action type, "count": n}, ...]}.
        """
        symbols = self._input_symbols(f.inputs, path)
        steps = [{"step": "INPUTS", "count": len(symbols)}]
        for action in f.actions:
            symbols = self.apply(action, symbols)
            steps.append({"step": action.type, "count": len(symbols)})
        return {"symbols": symbols, "steps": steps}

    def describe(self, symbols: set[str]) -> list[dict]:
        """Result rows with the prices at run time (kept in the run history for later analysis).

        Args:
            symbols: the matched tickers.

        Returns:
            Sorted [{ticker, id, name, sector, price, priceTime, dailyChangePercent}].
        """
        universe = self.market.universe()
        snaps = self._snaps(symbols)
        rows = []
        for t in sorted(symbols):
            info, snap = universe.get(t, {}), snaps.get(t, {})
            rows.append({
                "ticker": t,
                "id": info.get("id"),
                "name": info.get("name"),
                "sector": info.get("sector"),
                "price": snap.get("latestPrice"),
                "priceTime": snap.get("latestTime"),
                "dailyChangePercent": snap.get("dailyChangePercent"),
            })
        return rows
