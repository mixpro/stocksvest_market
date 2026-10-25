"""A MarketAPI that answers "as of" a past moment from a copy of the market database.

The filter engine and the order planner read every price through the MarketAPI functions, so pointing a MarketData at
HistoricalMarket runs them unchanged against history. Only data that was complete at `now` is used:

- the latest price is the close of the newest hourly bar that had ended (bar start + 1 hour <= now);
- a daily bar counts once its day's extended session has ended (20:00 New York);
- PRICE_CHANGE windows follow the market service's rules (DAYS are trading days, MINUTES / HOURS market time,
  WEEKS / MONTHS / YEARS calendar time), with the start price taken from a bar that had ended by the window start.

Live quotes are the latest price +/- half a modelled spread (see spread_for), so the planner's spread guard, ask/bid
pricing and requireLivePrice behave as they do live. The universe is today's halal list for the whole period
(stocks that left it earlier are missing: a survivorship bias the report mentions).

Used by: backtest.engine.
"""

import sqlite3
from bisect import bisect_right
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from stocksvest_market.api import _market_time_back, _trading_days_back

ET = ZoneInfo("America/New_York")
HOUR = 3600
# Spread model fitted on real IBKR quotes (Oct 22-23, 2026: 36 symbols) against their 20-day average dollar volume
# (log-log fit, R^2 0.43): spread% = SPREAD_A * ADV ** SPREAD_B, kept within [SPREAD_MIN, SPREAD_MAX].
# Used by: spread_for.
SPREAD_A, SPREAD_B = 370.0, -0.408
SPREAD_MIN, SPREAD_MAX = 0.01, 10.0
# How the market service counts periods. Used by: HistoricalMarket.price_change.
PERIOD = {"MINUTES": timedelta(minutes=1), "HOURS": timedelta(hours=1), "DAYS": timedelta(days=1),
          "WEEKS": timedelta(weeks=1), "MONTHS": timedelta(days=30.4375), "YEARS": timedelta(days=365.25)}


def spread_for(avg_dollar_volume: float | None) -> float:
    """Modelled bid/ask spread in percent for a stock's 20-day average dollar volume (thin stocks: wide spreads).

    Args:
        avg_dollar_volume: dollars traded a day; None or 0 = the widest spread.

    Used by: HistoricalMarket.quotes, engine (fit check).
    """
    if not avg_dollar_volume:
        return SPREAD_MAX
    return min(SPREAD_MAX, max(SPREAD_MIN, SPREAD_A * avg_dollar_volume ** SPREAD_B))


class Series:
    """One symbol's bars of one timeframe: parallel lists sorted by start time."""

    __slots__ = ("ts", "c", "v", "vw")

    def __init__(self):
        self.ts, self.c, self.v, self.vw = [], [], [], []


class HistoricalMarket:
    """MarketAPI over a market DB copy at a settable time `now` (an aware datetime).

    Used by: backtest.engine (wrapped in MarketData), tests.
    """

    def __init__(self, db_path: Path, start: date, end: date, hourly_lookback_days: int = 10):
        """Load symbols, calendar, daily bars (all) and hourly bars from `start` minus the lookback to `end`.

        Args:
            db_path: a COPY of market.db (never the live file: see the no-host-sqlite rule).
            start: first backtest day.
            end: last backtest day.
            hourly_lookback_days: hourly history loaded before `start` (HOURS windows look back into it).
        """
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        self.symbols_by_id = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM symbols")}
        self.calendar_rows = [dict(r) for r in conn.execute("SELECT * FROM calendar ORDER BY date")]
        self.trading_days_desc = [r["date"] for r in reversed(self.calendar_rows)]
        self.sessions_desc = [(datetime.fromisoformat(r["session_open"]), datetime.fromisoformat(r["session_close"]))
                              for r in reversed(self.calendar_rows)]
        self.day_close = {r["date"]: int(datetime.fromisoformat(r["session_close"]).timestamp())
                          for r in self.calendar_rows}
        self.daily = self._load(conn, "bars_1day", None)
        lo = int(datetime.combine(start - timedelta(days=hourly_lookback_days), time(), ET).timestamp())
        hi = int(datetime.combine(end + timedelta(days=1), time(), ET).timestamp())
        self.hourly = self._load(conn, "bars_1hour", (lo, hi))
        conn.close()
        self.by_ticker = {s["ticker"]: s for s in self.symbols_by_id.values()}
        self.now = datetime.combine(start, time(10), ET)
        self._adv: dict[tuple[str, str], float | None] = {}
        self.spread_factor = 1.0  # cost stress: modelled spreads x this (e.g. 2). Used by: quotes, next_fill

    def _load(self, conn, table: str, span: tuple[int, int] | None) -> dict[str, Series]:
        """Bars of a table by ticker.

        Args:
            conn: market DB connection.
            table: bars_1day or bars_1hour.
            span: (from, to) unix seconds, or None for all.
        """
        out: dict[str, Series] = {}
        sql = f"SELECT symbol_id, ts, c, v, vw FROM {table}"
        args: tuple = ()
        if span:
            sql += " WHERE ts >= ? AND ts < ?"
            args = span
        for sid, ts, c, v, vw in conn.execute(sql + " ORDER BY symbol_id, ts", args):
            sym = self.symbols_by_id.get(sid)
            if not sym:
                continue
            s = out.get(sym["ticker"])
            if s is None:
                s = out[sym["ticker"]] = Series()
            s.ts.append(ts)
            s.c.append(c)
            s.v.append(v)
            s.vw.append(vw if vw is not None else c)
        return out

    # -- as-of lookups

    def _now_ts(self) -> int:
        return int(self.now.timestamp())

    def _hourly_close_by(self, ticker: str, ts: int) -> tuple[int, float] | None:
        """(end time, close) of the newest hourly bar that had ended by `ts`."""
        s = self.hourly.get(ticker)
        if not s:
            return None
        i = bisect_right(s.ts, ts - HOUR) - 1
        return (s.ts[i] + HOUR, s.c[i]) if i >= 0 else None

    def _daily_index_by(self, ticker: str, ts: int) -> int | None:
        """Index of the newest daily bar whose day had ended (extended close) by `ts`."""
        s = self.daily.get(ticker)
        if not s:
            return None
        i = bisect_right(s.ts, ts) - 1
        while i >= 0:
            day = datetime.fromtimestamp(s.ts[i], ET).date().isoformat()
            if self.day_close.get(day, s.ts[i] + 86400) <= ts:
                return i
            i -= 1
        return None

    def _daily_on(self, ticker: str, day: str) -> float | None:
        """Close of the daily bar of that New York date, if there is one."""
        s = self.daily.get(ticker)
        if not s:
            return None
        midnight = int(datetime.combine(date.fromisoformat(day), time(), ET).timestamp())
        i = bisect_right(s.ts, midnight) - 1
        return s.c[i] if i >= 0 and datetime.fromtimestamp(s.ts[i], ET).date().isoformat() == day else None

    def latest_price(self, ticker: str) -> tuple[int, float] | None:
        """(time, price) at `now`: the newest ended hourly bar, else the newest ended daily bar."""
        ts = self._now_ts()
        hourly = self._hourly_close_by(ticker, ts)
        daily = None
        i = self._daily_index_by(ticker, ts)
        if i is not None:
            s = self.daily[ticker]
            daily = (self.day_close.get(datetime.fromtimestamp(s.ts[i], ET).date().isoformat(), s.ts[i]), s.c[i])
        if hourly and (not daily or hourly[0] >= daily[0]):
            return hourly
        return daily

    def avg_dollar_volume(self, ticker: str, days: int = 20) -> float | None:
        """Average dollars traded a day over the last `days` completed trading days (0 for days without trades)."""
        today = self.now.astimezone(ET).date().isoformat()
        key = (ticker, today, days)
        if key not in self._adv:
            self._adv[key] = self._volume(ticker, days, allow_short=True)[1]
        return self._adv[key]

    def _volume(self, ticker: str, days: int, allow_short: bool = False) -> tuple[float | None, float | None, int | None]:
        """(average shares, average dollars, days traded) over the last `days` completed trading days.

        Args:
            ticker: the symbol.
            days: trading days averaged.
            allow_short: average over the days there are when the calendar has fewer (the spread model); else
                None values, as the market service answers.
        """
        ts = self._now_ts()
        done = [d for d in self.trading_days_desc if self.day_close.get(d, 0) <= ts][:days]
        if not done or (len(done) < days and not allow_short):
            return None, None, None
        days = len(done)
        s = self.daily.get(ticker)
        if not s:
            return 0.0, 0.0, 0
        lo = int(datetime.combine(date.fromisoformat(done[-1]), time(), ET).timestamp())
        hi = int((datetime.combine(date.fromisoformat(done[0]), time(), ET) + timedelta(days=1)).timestamp())
        i, j = bisect_right(s.ts, lo - 1), bisect_right(s.ts, hi - 1)
        shares = sum(s.v[i:j])
        dollars = sum(v * w for v, w in zip(s.v[i:j], s.vw[i:j]))
        return shares / days, dollars / days, sum(1 for v in s.v[i:j] if v > 0)

    # -- MarketAPI functions (the subset the engine, the planner and MarketData call)

    def symbols(self, search=None, exchange=None, asset_type=None, sector=None, limit=None, offset=None) -> dict:
        rows = [{"id": s["id"], "ticker": s["ticker"], "name": s["name"], "exchange": s["exchange"],
                 "assetType": s["asset_type"], "sector": s.get("sector")} for s in self.symbols_by_id.values()]
        return {"total": len(rows), "symbols": sorted(rows, key=lambda r: r["ticker"])}

    def sectors(self) -> dict:
        counts: dict[str, int] = {}
        for s in self.symbols_by_id.values():
            if s.get("sector"):
                counts[s["sector"]] = counts.get(s["sector"], 0) + 1
        return {"sectors": [{"key": k, "name": k.title(), "symbols": n} for k, n in sorted(counts.items())],
                "unassigned": sum(1 for s in self.symbols_by_id.values() if not s.get("sector"))}

    def snapshots(self, symbols=None) -> dict:
        ts = self._now_ts()
        today = self.now.astimezone(ET).date().isoformat()
        prev_day = _trading_days_back(self.trading_days_desc, today, 1)
        out = {}
        for t in symbols or self.by_ticker:
            last = self.latest_price(t)
            prev = self._daily_on(t, prev_day) if prev_day else None
            s = self.hourly.get(t)
            day_start = int(datetime.combine(date.fromisoformat(today), time(), ET).timestamp())
            day_volume = None
            if s:
                i, j = bisect_right(s.ts, day_start - 1), bisect_right(s.ts, ts - HOUR)
                day_volume = sum(s.v[i:j]) if j > i else None
            change = last[1] - prev if last and prev else None
            out[t] = {"latestPrice": last[1] if last else None,
                      "latestTime": datetime.fromtimestamp(last[0], timezone.utc).isoformat() if last else None,
                      "dailyClose": last[1] if last else None, "dailyDate": today if day_volume else prev_day,
                      "dailyVolume": day_volume, "previousClose": prev, "dailyChange": change,
                      "dailyChangePercent": change / prev * 100 if change is not None and prev else None}
        return {"snapshots": out}

    def price_change(self, period_amount, period_unit, symbols=None) -> dict:
        ts = self._now_ts()
        span = PERIOD[period_unit] * period_amount
        out = {}
        for t in symbols or self.by_ticker:
            last = self.latest_price(t)
            base = None
            if last:
                end = datetime.fromtimestamp(last[0], timezone.utc)
                if period_unit == "DAYS":
                    day = _trading_days_back(self.trading_days_desc, end.astimezone(ET).date().isoformat(), period_amount)
                    base = self._daily_on(t, day) if day else None
                elif period_unit in ("MINUTES", "HOURS"):
                    start = _market_time_back(self.sessions_desc, end, span)
                    hit = self._hourly_close_by(t, int(start.timestamp())) if start else None
                    base = hit[1] if hit else None
                else:
                    i = self._daily_index_by(t, int((end - span).timestamp()))
                    base = self.daily[t].c[i] if i is not None else None
            change = last[1] - base if last and base else None
            out[t] = {"change": change, "changePercent": change / base * 100 if change is not None and base else None,
                      "toPrice": last[1] if last else None, "fromPrice": base}
        return {"periodAmount": period_amount, "periodUnit": period_unit, "changes": out, "asOf": ts}

    def volume(self, days, symbols=None) -> dict:
        out = {}
        for t in symbols or self.by_ticker:
            shares, dollars, traded = self._volume(t, days)
            out[t] = {"averageVolume": shares, "averageDollarVolume": dollars, "daysTraded": traded}
        return {"days": days, "volumes": out}

    def quotes(self, symbols) -> dict:
        out = {}
        for t in symbols:
            last = self.latest_price(t)
            if not last:
                continue
            half = spread_for(self.avg_dollar_volume(t)) * self.spread_factor / 200
            out[t] = {"source": "IBKR", "bid": round(last[1] * (1 - half), 4), "ask": round(last[1] * (1 + half), 4),
                      "last": last[1], "price": last[1], "time": self.now.isoformat(), "ageSeconds": 0}
        return {"quotes": out, "liveQuotes": {"enabled": True, "connected": True}}

    def next_fill(self, ticker: str, side: str) -> float | None:
        """Price a market order sent at `now` gets: the VWAP of the next hourly bar that started at or after `now`
        on the same day, at the modelled ask (BUY) or bid (SELL). None when the stock didn't trade again that day.
        Before the hourly history starts (only daily bars kept), the day's VWAP instead.

        Used by: engine._fill (fill mode NEXT_VWAP).
        """
        s = self.hourly.get(ticker)
        ts = self._now_ts()
        today = self.now.astimezone(ET).date()
        if not s or ts < s.ts[0]:
            d = self.daily.get(ticker)
            midnight = int(datetime.combine(today, time(), ET).timestamp())
            j = bisect_right(d.ts, midnight) - 1 if d else -1
            if j < 0 or datetime.fromtimestamp(d.ts[j], ET).date() != today:
                return None
            price = d.vw[j]
        else:
            i = bisect_right(s.ts, ts - 1)
            if i >= len(s.ts) or s.ts[i] >= self.day_close.get(today.isoformat(), ts + 86400):
                return None
            price = s.vw[i]
        half = spread_for(self.avg_dollar_volume(ticker)) * self.spread_factor / 200
        return round(price * (1 + half if side == "BUY" else 1 - half), 4)

    def calendar(self, start=None, end=None) -> dict:
        start = start or self.now.astimezone(ET).date().isoformat()
        end = end or (date.fromisoformat(start) + timedelta(days=30)).isoformat()
        return {"days": [{"date": r["date"], "preMarketOpen": r["session_open"], "regularOpen": r["open"],
                          "regularClose": r["close"], "afterHoursClose": r["session_close"],
                          "isEarlyClose": r["close"][11:16] != "16:00"}
                         for r in self.calendar_rows if start <= r["date"] <= end]}

    def status(self) -> dict:
        return {"lastIncrementalAt": self.now.isoformat(), "dataDelayMinutes": 0}
