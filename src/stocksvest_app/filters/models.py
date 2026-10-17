"""Request bodies for filters and groups, matching UML/FiltersPage.mmd."""

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

MAX_MINUTE_LOOKBACK = 7 * 24 * 60  # the market service keeps 7 days of minute bars


class InputType(str, Enum):
    UNIVERSE = "UNIVERSE"
    FILTER = "FILTER"
    FILTER_GROUP = "FILTER_GROUP"
    INVESTMENT_PORTFOLIO = "INVESTMENT_PORTFOLIO"
    WATCHLIST = "WATCHLIST"
    WATCHLIST_GROUP = "WATCHLIST_GROUP"


# Input types that exist in the app today; the rest arrive with watchlists and portfolios.
AVAILABLE_INPUTS = {InputType.UNIVERSE, InputType.FILTER, InputType.FILTER_GROUP}


class AssetType(str, Enum):
    COMMON_STOCK = "COMMON_STOCK"
    ADR = "ADR"


class Condition(str, Enum):
    LESS_THAN = "LESS_THAN"  # x < value
    BETWEEN = "BETWEEN"  # value <= x <= maxValue (inclusive)
    MORE_THAN = "MORE_THAN"  # x > value


class PriceReference(str, Enum):
    LAST_CLOSE = "LAST_CLOSE"  # close of the last completed trading day
    LATEST_BAR = "LATEST_BAR"  # newest price available (~16 min delayed)


class ValueType(str, Enum):
    PERCENT = "PERCENT"
    ABSOLUTE = "ABSOLUTE"


class TimePeriodUnit(str, Enum):
    MINUTES = "MINUTES"
    HOURS = "HOURS"
    DAYS = "DAYS"
    WEEKS = "WEEKS"
    MONTHS = "MONTHS"
    YEARS = "YEARS"


class FilterInput(BaseModel):
    type: InputType
    id: str | None = None

    @model_validator(mode="after")
    def check_id(self):
        if self.type == InputType.UNIVERSE and self.id:
            raise ValueError("UNIVERSE input has no id")
        if self.type != InputType.UNIVERSE and not self.id:
            raise ValueError(f"{self.type.value} input needs an id")
        return self


class Comparison(BaseModel):
    condition: Condition
    value: float
    maxValue: float | None = None

    @model_validator(mode="after")
    def check_bounds(self):
        if self.condition == Condition.BETWEEN:
            if self.maxValue is None:
                raise ValueError("BETWEEN needs maxValue")
            if self.maxValue < self.value:
                raise ValueError("maxValue must be >= value")
        elif self.maxValue is not None:
            raise ValueError("maxValue is only used with BETWEEN")
        return self

    def matches(self, x: float | None) -> bool:
        if x is None:
            return False
        if self.condition == Condition.LESS_THAN:
            return x < self.value
        if self.condition == Condition.MORE_THAN:
            return x > self.value
        return self.value <= x <= self.maxValue


class SymbolTypeAction(BaseModel):
    type: Literal["SYMBOL_TYPE"]
    assetTypes: list[AssetType] = Field(min_length=1)


class SectorAction(BaseModel):
    type: Literal["SECTOR"]
    sectorIds: list[str] = Field(min_length=1)


class SymbolPriceAction(Comparison):
    type: Literal["SYMBOL_PRICE"]
    priceReference: PriceReference


class PriceChangeAction(Comparison):
    type: Literal["PRICE_CHANGE"]
    periodAmount: int = Field(ge=1)
    periodUnit: TimePeriodUnit
    valueType: ValueType

    @model_validator(mode="after")
    def check_minutes(self):
        if self.periodUnit == TimePeriodUnit.MINUTES and self.periodAmount > MAX_MINUTE_LOOKBACK:
            raise ValueError(f"MINUTES periods are limited to {MAX_MINUTE_LOOKBACK} (7 days)")
        return self


FilterAction = Annotated[
    Union[SymbolTypeAction, SectorAction, SymbolPriceAction, PriceChangeAction], Field(discriminator="type")
]


class FilterIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    isActive: bool = True
    inputs: list[FilterInput] = Field(min_length=1)
    actions: list[FilterAction] = []


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    filterIds: list[str] = []
