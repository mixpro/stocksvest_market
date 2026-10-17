"""Request bodies for automations, matching UML/Automations.mmd and UML/Scheduling.mmd (phase 1 subset)."""

from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_TZ = "America/New_York"


class Weekday(str, Enum):
    MON = "MON"
    TUE = "TUE"
    WED = "WED"
    THU = "THU"
    FRI = "FRI"
    SAT = "SAT"
    SUN = "SUN"


WEEKDAY_INDEX = {d: i for i, d in enumerate(Weekday)}  # Monday = 0, like date.weekday()


class MissedPolicy(str, Enum):
    SKIP = "SKIP"  # runs missed while the app was down are dropped
    RUN_ONCE = "RUN_ONCE"  # one catch-up run for everything missed
    RUN_ALL = "RUN_ALL"  # one run per missed time (not allowed for automations that place orders)


class EndType(str, Enum):
    NEVER = "NEVER"
    UNTIL = "UNTIL"
    COUNT = "COUNT"


class EndCondition(BaseModel):
    type: EndType = EndType.NEVER
    until: datetime | None = None
    count: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def check(self):
        if self.type == EndType.UNTIL and self.until is None:
            raise ValueError("UNTIL needs until")
        if self.type == EndType.COUNT and self.count is None:
            raise ValueError("COUNT needs count")
        return self


class TimeWindow(BaseModel):
    start: time
    end: time

    @model_validator(mode="after")
    def check(self):
        if self.end <= self.start:
            raise ValueError("window end must be after start")
        return self


class ScheduleBase(BaseModel):
    id: str | None = None  # assigned on save
    timezone: str = DEFAULT_TZ
    startAt: datetime | None = None
    end: EndCondition = EndCondition()
    exceptions: list[date] = []  # dates (in the schedule's time zone) with no runs
    missedPolicy: MissedPolicy = MissedPolicy.RUN_ONCE

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone '{v}'")
        return v

    @field_validator("startAt")
    @classmethod
    def aware(cls, v):
        if v is not None and v.tzinfo is None:
            raise ValueError("startAt needs a time zone offset, e.g. 2026-10-20T09:30:00-04:00")
        return v


class OnceSchedule(ScheduleBase):
    type: Literal["ONCE"]
    at: datetime

    @field_validator("at")
    @classmethod
    def aware_at(cls, v):
        if v.tzinfo is None:
            raise ValueError("at needs a time zone offset, e.g. 2026-10-20T09:30:00-04:00")
        return v


class IntervalUnit(str, Enum):
    MINUTE = "MINUTE"
    HOUR = "HOUR"
    DAY = "DAY"


class IntervalSchedule(ScheduleBase):
    type: Literal["INTERVAL"]
    every: int = Field(ge=1)
    unit: IntervalUnit
    onDays: list[Weekday] = []  # empty = every day
    window: TimeWindow | None = None  # only fire inside this time of day

    @model_validator(mode="after")
    def check_minimum(self):
        if self.unit == IntervalUnit.MINUTE and self.every < 5:
            raise ValueError("the smallest interval is 5 minutes")
        return self


class DailySchedule(ScheduleBase):
    type: Literal["DAILY"]
    atTimes: list[time] = Field(min_length=1)


class WeeklySchedule(ScheduleBase):
    type: Literal["WEEKLY"]
    everyNWeeks: int = Field(default=1, ge=1)
    onDays: list[Weekday] = Field(min_length=1)
    atTimes: list[time] = Field(min_length=1)


class MarketAnchor(str, Enum):
    PRE_MARKET_OPEN = "PRE_MARKET_OPEN"
    REGULAR_OPEN = "REGULAR_OPEN"
    REGULAR_CLOSE = "REGULAR_CLOSE"
    AFTER_HOURS_CLOSE = "AFTER_HOURS_CLOSE"


class MarketSchedule(ScheduleBase):
    """Runs on trading days only, relative to the session times of that day (holidays and early closes included)."""

    type: Literal["MARKET"]
    anchor: MarketAnchor
    offsetMinutes: int = Field(default=0, ge=-240, le=240)
    repeatEveryMinutes: int | None = Field(default=None, ge=5)
    repeatUntil: MarketAnchor | None = None  # repeats strictly before this anchor; default REGULAR_CLOSE

    @model_validator(mode="after")
    def check_repeat(self):
        if self.repeatUntil and not self.repeatEveryMinutes:
            raise ValueError("repeatUntil needs repeatEveryMinutes")
        return self


Schedule = Annotated[
    Union[OnceSchedule, IntervalSchedule, DailySchedule, WeeklySchedule, MarketSchedule], Field(discriminator="type")
]


class RunTargetType(str, Enum):
    FILTER = "FILTER"
    FILTER_GROUP = "FILTER_GROUP"


class RunTarget(BaseModel):
    type: RunTargetType
    id: str


class NotifyAction(BaseModel):
    type: Literal["NOTIFY"]
    title: str = Field(min_length=1, max_length=200)
    message: str = ""
    includeMatchedSymbols: bool = True
    skipWhenNoMatches: bool = False


class LaterAction(BaseModel):
    """Action types from the design that arrive in later phases."""

    type: Literal["ADD_TO_WATCHLIST"]

    @model_validator(mode="after")
    def not_yet(self):
        raise ValueError(f"{self.type} actions are not available yet")


class SelectionMode(str, Enum):
    ALL_MATCHES = "ALL_MATCHES"
    TOP_N = "TOP_N"


class RankingKey(str, Enum):
    LAST_PRICE = "LAST_PRICE"
    DAILY_CHANGE_PERCENT = "DAILY_CHANGE_PERCENT"
    DAILY_VOLUME = "DAILY_VOLUME"
    POSITION_VALUE = "POSITION_VALUE"  # value of the automation's own shares
    UNREALIZED_PNL_PERCENT = "UNREALIZED_PNL_PERCENT"


class SymbolSelection(BaseModel):
    mode: SelectionMode = SelectionMode.ALL_MATCHES
    topN: int | None = Field(default=None, ge=1, le=100)
    rankBy: RankingKey | None = None
    descending: bool = True

    @model_validator(mode="after")
    def check(self):
        if self.mode == SelectionMode.TOP_N and (self.topN is None or self.rankBy is None):
            raise ValueError("TOP_N needs topN and rankBy")
        return self


class SizingMode(str, Enum):
    QUANTITY = "QUANTITY"  # shares
    CASH_AMOUNT = "CASH_AMOUNT"  # buy: money per symbol
    PERCENT_OF_BUDGET = "PERCENT_OF_BUDGET"  # buy: % of the budget available when the run starts
    PERCENT_OF_POSITION = "PERCENT_OF_POSITION"  # sell: % of the automation's shares of that symbol


class OrderSizing(BaseModel):
    mode: SizingMode
    value: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def check(self):
        if self.mode in (SizingMode.PERCENT_OF_BUDGET, SizingMode.PERCENT_OF_POSITION) and self.value > 100:
            raise ValueError("a percentage must be at most 100")
        return self


BUY_SIZINGS = {SizingMode.QUANTITY, SizingMode.CASH_AMOUNT, SizingMode.PERCENT_OF_BUDGET}
SELL_SIZINGS = {SizingMode.QUANTITY, SizingMode.PERCENT_OF_POSITION}


class PlaceOrderAction(BaseModel):
    """Turn the matched symbols into orders. ALL_MATCHES: one sizing for every symbol.
    TOP_N: one sizing for all ranks, or N sizings where sizings[i] is for rank i+1."""

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
    maxOrdersPerRun: int | None = Field(default=10, ge=1)
    maxPositionValue: Decimal | None = Field(default=None, gt=0)  # per symbol, the automation's shares
    maxPositionPercent: Decimal | None = Field(default=None, gt=0, le=100)  # of the automation's equity
    regularHoursOnly: bool = True
    skipBuyIfHeld: bool = True
    maxDataAgeMinutes: int = Field(default=30, ge=1)


Action = Annotated[Union[NotifyAction, PlaceOrderAction, LaterAction], Field(discriminator="type")]


class AutomationIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    portfolioId: str | None = None  # required for automations that place orders
    orderGuards: OrderGuards | None = None  # defaults apply when an automation places orders
    runTargets: list[RunTarget] = Field(min_length=1)
    schedules: list[Schedule] = Field(min_length=1)
    actions: list[Action] = Field(min_length=1)

    @model_validator(mode="after")
    def check_trading(self):
        if any(a.type == "PLACE_ORDER" for a in self.actions):
            if not self.portfolioId:
                raise ValueError("automations that place orders need a portfolioId")
            if any(s.missedPolicy == MissedPolicy.RUN_ALL for s in self.schedules):
                raise ValueError("missedPolicy RUN_ALL is not allowed for automations that place orders")
            if self.orderGuards is None:
                self.orderGuards = OrderGuards()
        return self


class BudgetChangeIn(BaseModel):
    amount: Decimal = Field(gt=0, decimal_places=2)
    note: str | None = None


class SwitchIn(BaseModel):
    automationsEnabled: bool
    reason: str | None = None
