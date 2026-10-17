"""Request bodies for automations, matching UML/Automations.mmd and UML/Scheduling.mmd (phase 1 subset)."""

from datetime import date, datetime, time
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

    type: Literal["ADD_TO_WATCHLIST", "PLACE_ORDER"]

    @model_validator(mode="after")
    def not_yet(self):
        raise ValueError(f"{self.type} actions are not available yet")


Action = Annotated[Union[NotifyAction, LaterAction], Field(discriminator="type")]


class AutomationIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    runTargets: list[RunTarget] = Field(min_length=1)
    schedules: list[Schedule] = Field(min_length=1)
    actions: list[Action] = Field(min_length=1)


class SwitchIn(BaseModel):
    automationsEnabled: bool
    reason: str | None = None
