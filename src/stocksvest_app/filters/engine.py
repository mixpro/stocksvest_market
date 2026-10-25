"""Evaluate filters: resolve inputs (union), then narrow the symbols with each action in order."""

import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from statistics import median
from zoneinfo import ZoneInfo

from ..market.client import LIVE_SOURCES, MarketError
from ..market.data import MarketData
from ..portfolios import store as portfolios_store
from . import store
from .models import (
    AVAILABLE_INPUTS,
    AverageVolumeAction,
    FilterAction,
    FilterIn,
    HoldingPeriodAction,
    MarketChangeAction,
    InputType,
    PortfolioIds,
    PositionAction,
    PriceChangeAction,
    PriceReference,
    ExcludeSymbolsAction,
    Indicator,
    IndicatorAction,
    SectorRelativeChangeAction,
    SectorAction,
    SymbolPriceAction,
    SymbolTypeAction,
    UnrealizedPnlAction,
    ValueType,
    VolumeType,
)

ET = ZoneInfo("America/New_York")  # Used by: Evaluator._last_close (today's date in New York)
# Seconds per HOLDING_PERIOD unit (calendar time; months and years as the market service counts them).
# Used by: Evaluator.apply
# INDICATOR -> field of market GET /v1/indicators. Used by: Evaluator.apply
INDICATOR_FIELDS = {Indicator.VOLATILITY_20: "volatility20", Indicator.ATR_14_PERCENT: "atr14Percent",
                    Indicator.PRICE_VS_SMA_50: "priceVsSma50Percent", Indicator.PRICE_VS_SMA_200: "priceVsSma200Percent",
                    Indicator.FROM_52W_HIGH: "fromHigh52wPercent", Indicator.ROE: "roe",
                    Indicator.NET_MARGIN: "netMargin", Indicator.DEBT_TO_ASSETS: "debtToAssets",
                    Indicator.DAYS_SINCE_EARNINGS: "daysSinceEarnings", Indicator.DAYS_TO_EARNINGS: "daysToEarnings"}
UNIT_SECONDS = {"MINUTES": 60, "HOURS": 3600, "DAYS": 86400, "WEEKS": 7 * 86400, "MONTHS": 30.4375 * 86400,
                "YEARS": 365.25 * 86400}
log = logging.getLogger(__name__)


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
    """Refuse a filter whose inputs reference missing filters/groups/portfolios or would create a cycle, or whose
    POSITION / UNREALIZED_PNL / HOLDING_PERIOD actions name missing portfolios.

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
        elif inp.type == InputType.INVESTMENT_PORTFOLIO and not portfolios_store.get_portfolio(conn, inp.id):
            raise EngineError(f"input portfolio '{inp.id}' does not exist")
    if filter_id and find_cycle(conn, ("F", filter_id), deps):
        raise EngineError("inputs would create a cycle (this filter would end up using itself)")
    for action in f.actions:
        if isinstance(action, PortfolioIds):
            for pid in action.portfolioIds:
                if not portfolios_store.get_portfolio(conn, pid):
                    raise EngineError(f"portfolio '{pid}' does not exist")


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
        self._live: dict[str, float] = {}  # ticker -> IBKR live last trade
        self._live_asked: set[str] = set()
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
            elif inp.type == InputType.INVESTMENT_PORTFOLIO:
                out |= self._held([inp.id])
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

    def _positions(self, portfolio_ids: list[str]) -> dict[str, dict]:
        """Open positions (any owner) of the portfolios combined per symbol.

        Args:
            portfolio_ids: the portfolios.

        Returns:
            {symbol: {"quantity": Decimal, "costBasis": Decimal}}, only symbols with shares.

        Raises:
            EngineError: a portfolio no longer exists.

        Used by: _held, apply (UNREALIZED_PNL).
        """
        out: dict[str, dict] = {}
        for pid in portfolio_ids:
            if not portfolios_store.get_portfolio(self.conn, pid):
                raise EngineError(f"portfolio '{pid}' does not exist")
            for p in portfolios_store.positions(self.conn, pid):
                if p["quantity"] > 0:
                    total = out.setdefault(p["symbol"], {"quantity": 0, "costBasis": 0})
                    total["quantity"] += p["quantity"]
                    total["costBasis"] += p["costBasis"]
        return out

    def _opened(self, portfolio_ids: list[str]) -> dict[str, datetime]:
        """When each held symbol's position was opened: its oldest open lot (any owner) in any of the portfolios.

        Args:
            portfolio_ids: the portfolios.

        Raises:
            EngineError: a portfolio no longer exists.

        Used by: apply (HOLDING_PERIOD).
        """
        out: dict[str, datetime] = {}
        for pid in portfolio_ids:
            if not portfolios_store.get_portfolio(self.conn, pid):
                raise EngineError(f"portfolio '{pid}' does not exist")
            for lot in portfolios_store.open_lots(self.conn, pid, any_owner=True):
                at = datetime.fromisoformat(lot["opened_at"].replace("Z", "+00:00"))
                if Decimal(lot["remaining_quantity"]) > 0 and (lot["symbol"] not in out or at < out[lot["symbol"]]):
                    out[lot["symbol"]] = at
        return out

    def _held(self, portfolio_ids: list[str]) -> set[str]:
        """Symbols with an open position (any owner) in any of the portfolios.

        Args:
            portfolio_ids: the portfolios.

        Raises:
            EngineError: a portfolio no longer exists.

        Used by: apply (POSITION), _input_symbols (INVESTMENT_PORTFOLIO).
        """
        return set(self._positions(portfolio_ids))

    def _live_last(self, tickers: set[str]) -> dict[str, float]:
        """IBKR live last trades for these tickers, asked once per evaluation; tickers without one (no live quote,
        no trade yet today, market service down) are left out so callers use the delayed price.

        Args:
            tickers: tickers needed.

        Used by: _unrealized_pnl.
        """
        missing = tickers - self._live_asked
        if missing:
            self._live_asked |= missing
            try:
                quotes = self.market.live_quotes(missing)
            except MarketError as e:
                log.warning("live quotes unavailable for filters, using delayed prices: %s", e.detail)
                quotes = {}
            self._live.update({t: q["last"] for t, q in quotes.items()
                               if q.get("source") in LIVE_SOURCES and q.get("last") is not None})
        return {t: self._live[t] for t in tickers if t in self._live}

    def _unrealized_pnl(self, action: UnrealizedPnlAction, symbols: set[str]) -> set[str]:
        """Keep the held symbols whose unrealized gain/loss matches the action, at the IBKR live last trade when
        there is one (like the portfolio page), else the delayed latest price.

        Args:
            action: the UNREALIZED_PNL action.
            symbols: the symbols still in the running.

        Used by: apply.
        """
        held = {t: p for t, p in self._positions(action.portfolioIds).items() if t in symbols}
        snaps = self._snaps(set(held))
        live = self._live_last(set(held))
        out = set()
        for t, p in held.items():
            price = live.get(t, snaps.get(t, {}).get("latestPrice"))
            if price is None:
                continue
            pnl = float(p["quantity"]) * price - float(p["costBasis"])
            if action.valueType == ValueType.PERCENT:
                pnl = pnl / float(p["costBasis"]) * 100 if p["costBasis"] else None
            if action.matches(pnl):
                out.add(t)
        return out

    def _market_change(self, amount: int, unit: str) -> float | None:
        """Median percent price change of the whole universe over the period (None without data).

        Used by: apply (MARKET_CHANGE).
        """
        changes = self.market.price_changes(set(self.market.universe()), amount, unit)
        values = [c["changePercent"] for c in changes.values() if c.get("changePercent") is not None]
        return median(values) if values else None

    def _sector_relative(self, action: SectorRelativeChangeAction, symbols: set[str]) -> set[str]:
        """Symbols whose % change minus their sector's median % change (whole universe) matches the action.

        Args:
            action: the SECTOR_RELATIVE_CHANGE action.
            symbols: the symbols still in the running.
        """
        universe = self.market.universe()
        changes = self.market.price_changes(set(universe), action.periodAmount, action.periodUnit.value)
        by_sector: dict[str, list[float]] = {}
        for t, c in changes.items():
            sector = universe.get(t, {}).get("sector")
            if sector and c.get("changePercent") is not None:
                by_sector.setdefault(sector, []).append(c["changePercent"])
        medians = {k: median(v) for k, v in by_sector.items()}
        out = set()
        for t in symbols:
            sector, pct = universe.get(t, {}).get("sector"), changes.get(t, {}).get("changePercent")
            if sector in medians and pct is not None and action.matches(pct - medians[sector]):
                out.add(t)
        return out

    def apply(self, action: FilterAction, symbols: set[str]) -> set[str]:
        """Keep the symbols that pass one action.

        Args:
            action: SymbolTypeAction, SectorAction, SymbolPriceAction, PriceChangeAction, AverageVolumeAction,
                PositionAction, UnrealizedPnlAction or HoldingPeriodAction.
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
        if isinstance(action, ExcludeSymbolsAction):
            return symbols - set(action.tickers)
        if isinstance(action, SymbolPriceAction):
            snaps = self._snaps(symbols)
            if action.priceReference == PriceReference.LATEST_BAR:
                return {t for t, s in snaps.items() if action.matches(s.get("latestPrice"))}
            return {t for t, s in snaps.items() if action.matches(self._last_close(s))}
        if isinstance(action, MarketChangeAction):  # before PriceChangeAction: it is one
            return symbols if action.matches(self._market_change(action.periodAmount, action.periodUnit.value)) else set()
        if isinstance(action, SectorRelativeChangeAction):  # also before PriceChangeAction
            return self._sector_relative(action, symbols)
        if isinstance(action, IndicatorAction):
            data = self.market.indicators(symbols)
            field = INDICATOR_FIELDS[action.indicator]
            return {t for t, v in data.items() if action.matches(v.get(field))}
        if isinstance(action, PriceChangeAction):
            changes = self.market.price_changes(symbols, action.periodAmount, action.periodUnit.value)
            field = "changePercent" if action.valueType == ValueType.PERCENT else "change"
            return {t for t, c in changes.items() if action.matches(c.get(field))}
        if isinstance(action, AverageVolumeAction):
            volumes = self.market.volumes(symbols, action.days)
            field = "averageVolume" if action.volumeType == VolumeType.SHARES else "averageDollarVolume"
            return {t for t, v in volumes.items() if action.matches(v.get(field))}
        if isinstance(action, PositionAction):
            return symbols & self._held(action.portfolioIds)
        if isinstance(action, UnrealizedPnlAction):
            return self._unrealized_pnl(action, symbols)
        if isinstance(action, HoldingPeriodAction):
            unit = UNIT_SECONDS[action.periodUnit.value]
            opened = self._opened(action.portfolioIds)
            return {t for t in symbols & set(opened)
                    if action.matches((self.now - opened[t]).total_seconds() / unit)}
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
