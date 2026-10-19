"""Request bodies for filters and groups, matching UML/FiltersPage.mmd."""

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

MAX_MINUTE_LOOKBACK = 7 * 24 * 60  # the market service keeps 7 days of minute bars. Used by: PriceChangeAction


class InputType(str, Enum):
    """Where a filter's starting symbols come from (all inputs are combined as a union).

    UNIVERSE: every halal symbol. FILTER / FILTER_GROUP: the live result of another filter or group (needs `id`).
    INVESTMENT_PORTFOLIO, WATCHLIST, WATCHLIST_GROUP: from the design, not available yet (refused at run time).

    Used by: FilterInput.type, engine.Evaluator._input_symbols, engine.check_filter_refs, routes.filter_actions.
    """

    UNIVERSE = "UNIVERSE"
    FILTER = "FILTER"
    FILTER_GROUP = "FILTER_GROUP"
    INVESTMENT_PORTFOLIO = "INVESTMENT_PORTFOLIO"
    WATCHLIST = "WATCHLIST"
    WATCHLIST_GROUP = "WATCHLIST_GROUP"


# Input types that exist in the app today; the rest arrive with watchlists and portfolios.
# Used by: engine.Evaluator._input_symbols (refuses others), routes.filter_actions ("available" flag).
AVAILABLE_INPUTS = {InputType.UNIVERSE, InputType.FILTER, InputType.FILTER_GROUP}


class AssetType(str, Enum):
    """Kinds of security in the halal universe (the market service excludes ETFs, preferred, warrants, units and
    rights). Matches the market's symbols.asset_type.

    Used by: SymbolTypeAction.assetTypes, engine.Evaluator.apply, routes.filter_actions.
    """

    COMMON_STOCK = "COMMON_STOCK"
    ADR = "ADR"


class Condition(str, Enum):
    """How a number is compared in price and price-change actions. LESS_THAN / MORE_THAN are strict;
    BETWEEN is inclusive and needs maxValue.

    Used by: Comparison (SymbolPriceAction, PriceChangeAction), routes.filter_actions.
    """

    LESS_THAN = "LESS_THAN"  # x < value
    BETWEEN = "BETWEEN"  # value <= x <= maxValue (inclusive)
    MORE_THAN = "MORE_THAN"  # x > value


class PriceReference(str, Enum):
    """Which price SYMBOL_PRICE compares.

    Used by: SymbolPriceAction.priceReference, engine.Evaluator.apply / _last_close, routes.filter_actions.
    """

    LAST_CLOSE = "LAST_CLOSE"  # close of the last completed trading day
    LATEST_BAR = "LATEST_BAR"  # newest price available (~16 min delayed)


class ValueType(str, Enum):
    """Whether PRICE_CHANGE compares the change in percent (3 = 3%) or in dollars.

    Used by: PriceChangeAction.valueType, engine.Evaluator.apply, routes.filter_actions.
    """

    PERCENT = "PERCENT"
    ABSOLUTE = "ABSOLUTE"


class TimePeriodUnit(str, Enum):
    """Unit of the PRICE_CHANGE look-back; sent unchanged to the market's GET /v1/price-change (its PeriodUnit).
    MINUTES is limited to MAX_MINUTE_LOOKBACK.

    Used by: PriceChangeAction.periodUnit, routes.filter_actions.
    """

    MINUTES = "MINUTES"
    HOURS = "HOURS"
    DAYS = "DAYS"
    WEEKS = "WEEKS"
    MONTHS = "MONTHS"
    YEARS = "YEARS"


class FilterInput(BaseModel):
    """One input of a filter: {"type": "UNIVERSE"} or {"type": "FILTER" | "FILTER_GROUP", "id": "..."}.

    Used by: FilterIn.inputs; engine (evaluation and reference/cycle checks).
    """

    type: InputType
    id: str | None = None

    @model_validator(mode="after")
    def check_id(self):
        """UNIVERSE takes no id; every other type needs one."""
        if self.type == InputType.UNIVERSE and self.id:
            raise ValueError("UNIVERSE input has no id")
        if self.type != InputType.UNIVERSE and not self.id:
            raise ValueError(f"{self.type.value} input needs an id")
        return self


class Comparison(BaseModel):
    """Shared fields of actions that compare a number: condition, value and (for BETWEEN) maxValue.

    Used by: SymbolPriceAction, PriceChangeAction (base class); engine.Evaluator.apply (matches).
    """

    condition: Condition
    value: float
    maxValue: float | None = None

    @model_validator(mode="after")
    def check_bounds(self):
        """BETWEEN needs maxValue >= value; other conditions must not set maxValue."""
        if self.condition == Condition.BETWEEN:
            if self.maxValue is None:
                raise ValueError("BETWEEN needs maxValue")
            if self.maxValue < self.value:
                raise ValueError("maxValue must be >= value")
        elif self.maxValue is not None:
            raise ValueError("maxValue is only used with BETWEEN")
        return self

    def matches(self, x: float | None) -> bool:
        """Does a value pass the comparison?

        Args:
            x: the symbol's value (price, change); None (no data) never matches.
        """
        if x is None:
            return False
        if self.condition == Condition.LESS_THAN:
            return x < self.value
        if self.condition == Condition.MORE_THAN:
            return x > self.value
        return self.value <= x <= self.maxValue


class SymbolTypeAction(BaseModel):
    """Keep symbols whose asset type is in `assetTypes`. Used by: FilterAction, engine.Evaluator.apply."""

    type: Literal["SYMBOL_TYPE"]
    assetTypes: list[AssetType] = Field(min_length=1)


class SectorAction(BaseModel):
    """Keep symbols whose sector key (market GET /v1/sectors) is in `sectorIds`.
    Used by: FilterAction, engine.Evaluator.apply."""

    type: Literal["SECTOR"]
    sectorIds: list[str] = Field(min_length=1)


class SymbolPriceAction(Comparison):
    """Keep symbols whose price (priceReference) matches the comparison.
    Used by: FilterAction, engine.Evaluator.apply."""

    type: Literal["SYMBOL_PRICE"]
    priceReference: PriceReference


class PriceChangeAction(Comparison):
    """Keep symbols whose price change over periodAmount x periodUnit (valueType: percent or dollars) matches the
    comparison. Used by: FilterAction, engine.Evaluator.apply (via MarketData.price_changes)."""

    type: Literal["PRICE_CHANGE"]
    periodAmount: int = Field(ge=1)
    periodUnit: TimePeriodUnit
    valueType: ValueType

    @model_validator(mode="after")
    def check_minutes(self):
        """MINUTES look-backs must fit in the minute history the market keeps."""
        if self.periodUnit == TimePeriodUnit.MINUTES and self.periodAmount > MAX_MINUTE_LOOKBACK:
            raise ValueError(f"MINUTES periods are limited to {MAX_MINUTE_LOOKBACK} (7 days)")
        return self


class PositionAction(BaseModel):
    """Keep symbols held (open position, any owner: manual or automation) in at least one of the portfolios.

    Attributes:
        portfolioIds: one or more portfolios (union of their positions); each at most once.

    Used by: FilterAction, engine.Evaluator.apply, engine.check_filter_refs (portfolios must exist),
    filters.store.portfolio_users (portfolio delete guard).
    """

    type: Literal["POSITION"]
    portfolioIds: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def check_unique(self):
        """Each portfolio at most once."""
        if len(set(self.portfolioIds)) != len(self.portfolioIds):
            raise ValueError("a portfolio is listed twice")
        return self


# Any filter action; the "type" field picks the model. Used by: FilterIn.actions, engine.Evaluator.apply.
FilterAction = Annotated[
    Union[SymbolTypeAction, SectorAction, SymbolPriceAction, PriceChangeAction, PositionAction],
    Field(discriminator="type"),
]


class FilterIn(BaseModel):
    """A filter as clients send it (create, update, preview) and as stored filters are re-validated for runs.

    Attributes:
        name: 1-100 characters.
        description: free text.
        isActive: shown in the UI; reserved for automations.
        inputs: at least one FilterInput (union).
        actions: FilterAction list applied in order; empty keeps every input symbol.

    Used by: routes (create/update/preview/run), engine (Evaluator.evaluate, check_filter_refs),
    automations.runner (run targets).
    """

    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    isActive: bool = True
    inputs: list[FilterInput] = Field(min_length=1)
    actions: list[FilterAction] = []


class GroupIn(BaseModel):
    """A filter group: named list of filters whose results are combined as a union.

    Attributes:
        name: 1-100 characters.
        description: free text.
        filterIds: member filters, in order; each at most once.

    Used by: routes (create/update group), engine.check_group_refs.
    """

    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    filterIds: list[str] = []
