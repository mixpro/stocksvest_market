"""Request bodies for automations, matching UML/Automations.mmd and UML/Scheduling.mmd (phase 1 subset)."""

from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_TZ = "America/New_York"  # Used by: ScheduleBase.timezone default


class Weekday(str, Enum):
    """Days of the week for WEEKLY and INTERVAL schedules.

    Used by: WeeklySchedule.onDays, IntervalSchedule.onDays, WEEKDAY_INDEX, timing (_raw_fires, _allowed).
    """
    MON = "MON"
    TUE = "TUE"
    WED = "WED"
    THU = "THU"
    FRI = "FRI"
    SAT = "SAT"
    SUN = "SUN"


WEEKDAY_INDEX = {d: i for i, d in enumerate(Weekday)}  # Monday = 0, like date.weekday(). Used by: timing


class MissedPolicy(str, Enum):
    """What the scheduler does with times that passed while the app was down or busy (more than 2 minutes late).

    RUN_ONCE: one catch-up run for all of them. SKIP: none (recorded as one SKIPPED / MISSED run).
    RUN_ALL: one run per missed time, up to 50 (not allowed for automations that place orders).

    Used by: ScheduleBase.missedPolicy, AutomationIn.check_trading, scheduler._tick_schedule.
    """
    SKIP = "SKIP"  # runs missed while the app was down are dropped
    RUN_ONCE = "RUN_ONCE"  # one catch-up run for everything missed
    RUN_ALL = "RUN_ALL"  # one run per missed time (not allowed for automations that place orders)


class EndType(str, Enum):
    """When a schedule stops: NEVER, UNTIL a time, or after COUNT fires.

    Used by: EndCondition.type, timing.iter_fires (UNTIL), timing.next_fires and scheduler (COUNT).
    """
    NEVER = "NEVER"
    UNTIL = "UNTIL"
    COUNT = "COUNT"


class EndCondition(BaseModel):
    """A schedule's end: {"type": "NEVER"} | {"type": "UNTIL", "until": time} | {"type": "COUNT", "count": n}.

    Used by: ScheduleBase.end.
    """
    type: EndType = EndType.NEVER
    until: datetime | None = None
    count: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def check(self):
        """UNTIL needs `until`; COUNT needs `count`."""
        if self.type == EndType.UNTIL and self.until is None:
            raise ValueError("UNTIL needs until")
        if self.type == EndType.COUNT and self.count is None:
            raise ValueError("COUNT needs count")
        return self


class TimeWindow(BaseModel):
    """Time of day range (in the schedule's time zone) an INTERVAL schedule may fire in; both ends included.

    Used by: IntervalSchedule.window, timing._allowed.
    """
    start: time
    end: time

    @model_validator(mode="after")
    def check(self):
        """The end must be after the start."""
        if self.end <= self.start:
            raise ValueError("window end must be after start")
        return self


class ScheduleBase(BaseModel):
    """Fields every schedule has.

    Attributes:
        id: stable id, assigned when the automation is saved (kept across edits).
        timezone: IANA zone the times are in (default New York); MARKET schedules always use New York.
        startAt: no fires before this time (needs an offset).
        end: EndCondition.
        exceptions: dates (in the schedule's zone) with no fires.
        missedPolicy: MissedPolicy.

    Used by: every schedule type below; timing, scheduler.
    """
    id: str | None = None  # assigned on save
    timezone: str = DEFAULT_TZ
    startAt: datetime | None = None
    end: EndCondition = EndCondition()
    exceptions: list[date] = []  # dates (in the schedule's time zone) with no runs
    missedPolicy: MissedPolicy = MissedPolicy.RUN_ONCE

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, v: str) -> str:
        """The time zone must exist.

        Args:
            v: zone name, e.g. "Asia/Jerusalem".
        """
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone '{v}'")
        return v

    @field_validator("startAt")
    @classmethod
    def aware(cls, v):
        """startAt must carry a time zone offset.

        Args:
            v: the value.
        """
        if v is not None and v.tzinfo is None:
            raise ValueError("startAt needs a time zone offset, e.g. 2026-10-20T09:30:00-04:00")
        return v


class OnceSchedule(ScheduleBase):
    """Fires once, at `at` (with an offset). Used by: Schedule, timing._raw_fires."""
    type: Literal["ONCE"]
    at: datetime

    @field_validator("at")
    @classmethod
    def aware_at(cls, v):
        """`at` must carry a time zone offset.

        Args:
            v: the value.
        """
        if v.tzinfo is None:
            raise ValueError("at needs a time zone offset, e.g. 2026-10-20T09:30:00-04:00")
        return v


class IntervalUnit(str, Enum):
    """Step unit of an INTERVAL schedule. MINUTE/HOUR steps are exact; DAY steps follow the wall clock (DST-safe).

    Used by: IntervalSchedule.unit, timing._raw_fires.
    """
    MINUTE = "MINUTE"
    HOUR = "HOUR"
    DAY = "DAY"


class IntervalSchedule(ScheduleBase):
    """Fires every `every` x `unit` from startAt (or the automation's creation), optionally only on `onDays` and
    inside `window`. The smallest interval is 1 minute. Used by: Schedule, timing.
    """
    type: Literal["INTERVAL"]
    every: int = Field(ge=1)
    unit: IntervalUnit
    onDays: list[Weekday] = []  # empty = every day
    window: TimeWindow | None = None  # only fire inside this time of day


class DailySchedule(ScheduleBase):
    """Fires every day at each of `atTimes`. Used by: Schedule, timing._raw_fires."""
    type: Literal["DAILY"]
    atTimes: list[time] = Field(min_length=1)


class WeeklySchedule(ScheduleBase):
    """Fires on `onDays` at each of `atTimes`, every `everyNWeeks` weeks (counted from startAt or creation).
    Used by: Schedule, timing._raw_fires.
    """
    type: Literal["WEEKLY"]
    everyNWeeks: int = Field(default=1, ge=1)
    onDays: list[Weekday] = Field(min_length=1)
    atTimes: list[time] = Field(min_length=1)


class MarketAnchor(str, Enum):
    """Session time a MARKET schedule is relative to (that trading day's own times, so early closes count).

    Used by: MarketSchedule.anchor / repeatUntil, timing.ANCHOR_FIELD.
    """
    PRE_MARKET_OPEN = "PRE_MARKET_OPEN"
    REGULAR_OPEN = "REGULAR_OPEN"
    REGULAR_CLOSE = "REGULAR_CLOSE"
    AFTER_HOURS_CLOSE = "AFTER_HOURS_CLOSE"


class MarketSchedule(ScheduleBase):
    """Fires on trading days only, at anchor + offsetMinutes, then (optionally) every repeatEveryMinutes until just
    before repeatUntil (default the regular close). Holidays come from the market calendar.

    Used by: Schedule, timing._raw_fires (via MarketData.calendar_days).
    """

    type: Literal["MARKET"]
    anchor: MarketAnchor
    offsetMinutes: int = Field(default=0, ge=-240, le=240)
    repeatEveryMinutes: int | None = Field(default=None, ge=1)
    repeatUntil: MarketAnchor | None = None  # repeats strictly before this anchor; default REGULAR_CLOSE

    @model_validator(mode="after")
    def check_repeat(self):
        """repeatUntil only makes sense with repeatEveryMinutes."""
        if self.repeatUntil and not self.repeatEveryMinutes:
            raise ValueError("repeatUntil needs repeatEveryMinutes")
        return self


# Any schedule; the "type" field picks the model. Used by: AutomationIn.schedules, timing, scheduler, routes.
Schedule = Annotated[
    Union[OnceSchedule, IntervalSchedule, DailySchedule, WeeklySchedule, MarketSchedule], Field(discriminator="type")
]


class RunTargetType(str, Enum):
    """What an automation runs: a filter or a filter group. Used by: RunTarget, runner._run_targets."""
    FILTER = "FILTER"
    FILTER_GROUP = "FILTER_GROUP"


class RunTarget(BaseModel):
    """One filter or group an automation runs; the matched symbols of all targets are combined as a union.
    Used by: AutomationIn.runTargets, runner._run_targets, routes.prepare (existence check).
    """
    type: RunTargetType
    id: str


class NotifyAction(BaseModel):
    """Send an in-app notification. In title and message, {name} = automation name, {count} = matches.

    Attributes:
        title: required.
        message: optional text.
        includeMatchedSymbols: list the matched tickers in the notification.
        skipWhenNoMatches: send nothing (action SKIPPED) when there are no matches.

    Used by: Action, runner.run_automation.
    """
    type: Literal["NOTIFY"]
    title: str = Field(min_length=1, max_length=200)
    message: str = ""
    includeMatchedSymbols: bool = True
    skipWhenNoMatches: bool = False


class LaterAction(BaseModel):
    """Action types from the design that arrive later (ADD_TO_WATCHLIST); always refused with "not available yet".
    Used by: Action.
    """

    type: Literal["ADD_TO_WATCHLIST"]

    @model_validator(mode="after")
    def not_yet(self):
        """Always refuse."""
        raise ValueError(f"{self.type} actions are not available yet")


class SelectionMode(str, Enum):
    """Which matches a PLACE_ORDER action trades: ALL_MATCHES (one sizing each) or the TOP_N by a ranking key.

    Used by: SymbolSelection.mode, PlaceOrderAction.check, planner.plan.
    """
    ALL_MATCHES = "ALL_MATCHES"
    TOP_N = "TOP_N"


class RankingKey(str, Enum):
    """How TOP_N ranks the matches (descending by default). POSITION_VALUE and UNREALIZED_PNL_PERCENT use the
    budget's shares (unheld symbols rank last). MOMENTUM_12_1 / MOMENTUM_6_1: the price change from 12 (6) months ago
    to 1 month ago, leaving out the latest month, which tends to reverse. AVERAGE_DOLLAR_VOLUME_20: dollars traded a
    day over the last 20 trading days, the most-traded (usually largest) companies first. VOLATILITY_20 and
    FROM_52W_HIGH come from market GET /v1/indicators. planner.add_rank_data fetches all of these.

    Used by: SymbolSelection.rankBy, planner._rank_value.
    """
    LAST_PRICE = "LAST_PRICE"
    DAILY_CHANGE_PERCENT = "DAILY_CHANGE_PERCENT"
    DAILY_VOLUME = "DAILY_VOLUME"
    POSITION_VALUE = "POSITION_VALUE"  # value of the budget's shares
    UNREALIZED_PNL_PERCENT = "UNREALIZED_PNL_PERCENT"
    MOMENTUM_12_1 = "MOMENTUM_12_1"  # % change from 12 months ago to 1 month ago (the latest month left out)
    MOMENTUM_6_1 = "MOMENTUM_6_1"  # same from 6 months ago
    AVERAGE_DOLLAR_VOLUME_20 = "AVERAGE_DOLLAR_VOLUME_20"  # dollars traded a day over the last 20 trading days
    VOLATILITY_20 = "VOLATILITY_20"  # annualised 20-day volatility, % (descending false = calmest first)
    FROM_52W_HIGH = "FROM_52W_HIGH"  # close vs 52-week high, % (descending = nearest the high first)


class SymbolSelection(BaseModel):
    """Which matched symbols an order action uses. Used by: PlaceOrderAction.selection, planner.plan."""
    mode: SelectionMode = SelectionMode.ALL_MATCHES
    topN: int | None = Field(default=None, ge=1, le=100)
    rankBy: RankingKey | None = None
    descending: bool = True

    @model_validator(mode="after")
    def check(self):
        """TOP_N needs topN and rankBy."""
        if self.mode == SelectionMode.TOP_N and (self.topN is None or self.rankBy is None):
            raise ValueError("TOP_N needs topN and rankBy")
        return self


class SizingMode(str, Enum):
    """How big each order is. Buys: QUANTITY (shares), CASH_AMOUNT (dollars), PERCENT_OF_BUDGET (of the budget
    available when the run starts). Sells: QUANTITY, PERCENT_OF_POSITION (of the budget's shares).
    Quantities are rounded down to whole shares.

    Used by: OrderSizing.mode, BUY_SIZINGS / SELL_SIZINGS, planner.plan.
    """
    QUANTITY = "QUANTITY"  # shares
    CASH_AMOUNT = "CASH_AMOUNT"  # buy: money per symbol
    PERCENT_OF_BUDGET = "PERCENT_OF_BUDGET"  # buy: % of the budget available when the run starts
    PERCENT_OF_POSITION = "PERCENT_OF_POSITION"  # sell: % of the budget's shares of that symbol


class OrderSizing(BaseModel):
    """One sizing: mode + value (> 0; percentages <= 100). Used by: PlaceOrderAction.sizings, planner.plan."""
    mode: SizingMode
    value: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def check(self):
        """Percentages can't exceed 100."""
        if self.mode in (SizingMode.PERCENT_OF_BUDGET, SizingMode.PERCENT_OF_POSITION) and self.value > 100:
            raise ValueError("a percentage must be at most 100")
        return self


# Which sizing modes each side accepts. Used by: PlaceOrderAction.check.
BUY_SIZINGS = {SizingMode.QUANTITY, SizingMode.CASH_AMOUNT, SizingMode.PERCENT_OF_BUDGET}
SELL_SIZINGS = {SizingMode.QUANTITY, SizingMode.PERCENT_OF_POSITION}


class PlaceOrderAction(BaseModel):
    """Turn the matched symbols into orders. ALL_MATCHES: one sizing for every symbol.
    TOP_N: one sizing for all ranks, or N sizings where sizings[i] is for rank i+1.

    Attributes:
        side: BUY, or SELL (only shares bought with the same budget, by any of its automations).
        orderType: MARKET, LIMIT, STOP, STOP_LIMIT or TRAILING_STOP.
        timeInForce: DAY, GTC, OPG, CLS, IOC or FOK (checked against the broker's capabilities).
        limitOffsetPercent: limit price = latest price x (1 + offset%); required for LIMIT / STOP_LIMIT.
        stopOffsetPercent: stop price likewise; required for STOP / STOP_LIMIT.
        trailPercent: required for TRAILING_STOP.
        extendedHours: only with LIMIT + DAY.
        selection: SymbolSelection.
        sizings: OrderSizing list (see above).

    Used by: Action, AutomationIn.check_trading, runner._place_orders, planner.select / size.
    """

    type: Literal["PLACE_ORDER"]
    side: Literal["BUY", "SELL"]
    orderType: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP"] = "MARKET"
    timeInForce: Literal["DAY", "GTC", "OPG", "CLS", "IOC", "FOK"] = "DAY"
    limitOffsetPercent: Decimal | None = Field(default=None, ge=-50, le=50)  # limit = price x (1 + offset%)
    stopOffsetPercent: Decimal | None = Field(default=None, ge=-50, le=50)
    trailPercent: Decimal | None = Field(default=None, gt=0, le=50)
    extendedHours: bool = False
    selection: SymbolSelection = SymbolSelection()
    sizings: list[OrderSizing] = Field(min_length=1)

    @model_validator(mode="after")
    def check(self):
        """Sizing modes fit the side and selection; order types have the prices they need."""
        allowed = BUY_SIZINGS if self.side == "BUY" else SELL_SIZINGS
        bad = [s.mode.value for s in self.sizings if s.mode not in allowed]
        if bad:
            raise ValueError(f"{self.side} orders can't be sized by {', '.join(bad)}")
        if self.selection.mode == SelectionMode.ALL_MATCHES and len(self.sizings) != 1:
            raise ValueError("ALL_MATCHES takes exactly one sizing")
        if self.selection.mode == SelectionMode.TOP_N and len(self.sizings) not in (1, self.selection.topN):
            raise ValueError(f"TOP_N takes 1 sizing or {self.selection.topN} (one per rank)")
        if self.orderType in ("LIMIT", "STOP_LIMIT") and self.limitOffsetPercent is None:
            raise ValueError(f"{self.orderType} needs limitOffsetPercent")
        if self.orderType in ("STOP", "STOP_LIMIT") and self.stopOffsetPercent is None:
            raise ValueError(f"{self.orderType} needs stopOffsetPercent")
        if self.orderType == "TRAILING_STOP" and self.trailPercent is None:
            raise ValueError("TRAILING_STOP needs trailPercent")
        if self.extendedHours and not (self.orderType == "LIMIT" and self.timeInForce == "DAY"):
            raise ValueError("extended hours need a LIMIT + DAY order")
        return self


class OrderGuards(BaseModel):
    """Limits applied to every order an automation plans (defaults shown in the API docs). They apply to each budget
    the automation trades with separately.

    Attributes:
        maxOrdersPerRun: orders per budget across all order actions of one run.
        maxPositionValue: cap per symbol on the budget's shares, in dollars (buys shrink to fit).
        maxPositionPercent: same cap as a % of the budget's equity (cash + invested).
        regularHoursOnly: place nothing outside the regular session.
        skipBuyIfHeld: no buy when the budget holds the symbol or has a buy open (from any of its automations).
        maxDataAgeMinutes: place nothing when the market data is older than this.
        requireLivePrice: place an order only with a live quote (Alpaca or IBKR) for its symbol; otherwise skip it
            (NO_LIVE_PRICE) instead of using the newest minute bar.
        maxSpreadPercent: skip a buy (WIDE_SPREAD) when its live quote's ask is more than this % above the bid,
            measured from the midpoint; thinly traded stocks fill at the ask, far above their value. Needs a live
            quote (symbols without one are not checked); sells are never held back by it.
        rebuyCooldownMinutes: no buy of a symbol the budget sold (by any of its automations) less than this many
            minutes ago (RECENTLY_SOLD); a TOP_N buy leaves them out before taking the top N. Stops a buy and a
            sell automation from trading the same stock back and forth.
        maxSectorPercent: cap on one sector's share of the budget's equity (its holdings plus this run's buys).
            A TOP_N buy leaves out stocks of full sectors before taking the top N (sectorFull), counting each
            planned buy at maxPositionPercent; a buy that would still cross the cap shrinks or skips (SECTOR_FULL).
            Stocks without a sector are not capped. Holdings that grew past the cap are not sold.
        targetVolatilityPercent: volatility-scaled buys: each buy and its position caps are multiplied by
            target / the stock's 20-day annualised volatility, kept within 0.5-1.5 (calm stocks get bigger
            positions, jumpy ones smaller). 40 with maxPositionPercent 5 = 2.5-7.5% per stock. Stocks without
            enough history are not scaled.
        maxOpenPositions: most symbols the budget may hold at once (holdings, open buys and this run's buys); a
            buy of a new symbol beyond it skips (MAX_POSITIONS). Adding to a held symbol is not limited.

    Used by: AutomationIn.orderGuards, runner.Trading, planner.plan.
    """
    maxOrdersPerRun: int | None = Field(default=10, ge=1)
    maxPositionValue: Decimal | None = Field(default=None, gt=0)  # per symbol, the budget's shares
    maxPositionPercent: Decimal | None = Field(default=None, gt=0, le=100)  # of the budget's equity
    regularHoursOnly: bool = True
    skipBuyIfHeld: bool = True
    maxDataAgeMinutes: int = Field(default=30, ge=1)
    requireLivePrice: bool = False
    maxSpreadPercent: Decimal | None = Field(default=None, gt=0, le=100)  # buys: (ask - bid) / midpoint
    rebuyCooldownMinutes: int | None = Field(default=None, ge=1, le=43200)  # up to 30 days
    maxSectorPercent: Decimal | None = Field(default=None, gt=0, le=100)  # of the budget's equity
    targetVolatilityPercent: Decimal | None = Field(default=None, gt=0, le=200)  # annualised, %
    maxOpenPositions: int | None = Field(default=None, ge=1, le=1000)


# Any automation action; the "type" field picks the model. Used by: AutomationIn.actions, runner.run_automation.
Action = Annotated[Union[NotifyAction, PlaceOrderAction, LaterAction], Field(discriminator="type")]


class AutomationIn(BaseModel):
    """An automation as clients send it (create, update, preview). Which money it trades with is not part of it:
    portfolios attach automations to their budgets (budgets.routes).

    Attributes:
        name: 1-100 characters.
        description: free text.
        orderGuards: OrderGuards; defaults applied when an action places orders.
        runTargets: at least one RunTarget (union).
        schedules: at least one Schedule.
        actions: at least one Action, run in order.

    Used by: routes (create, update, preview), runner (via the stored definition).
    """
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    orderGuards: OrderGuards | None = None  # defaults apply when an automation places orders
    runTargets: list[RunTarget] = Field(min_length=1)
    schedules: list[Schedule] = Field(min_length=1)
    actions: list[Action] = Field(min_length=1)

    @model_validator(mode="after")
    def check_trading(self):
        """Order-placing automations can't use RUN_ALL, and get default guards."""
        if any(a.type == "PLACE_ORDER" for a in self.actions):
            if any(s.missedPolicy == MissedPolicy.RUN_ALL for s in self.schedules):
                raise ValueError("missedPolicy RUN_ALL is not allowed for automations that place orders")
            if self.orderGuards is None:
                self.orderGuards = OrderGuards()
        return self


class SwitchIn(BaseModel):
    """Body of PUT /v1/automations/switch: turn scheduled runs on/off, with an optional reason.
    Used by: routes.set_switch.
    """
    automationsEnabled: bool
    reason: str | None = None
