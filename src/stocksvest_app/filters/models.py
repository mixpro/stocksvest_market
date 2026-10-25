"""Request bodies for filters and groups, matching UML/FiltersPage.mmd."""

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

MAX_MINUTE_LOOKBACK = 7 * 24 * 60  # the market service keeps 7 days of minute bars. Used by: PriceChangeAction


class InputType(str, Enum):
    """Where a filter's starting symbols come from (all inputs are combined as a union).

    UNIVERSE: every halal symbol. FILTER / FILTER_GROUP: the live result of another filter or group (needs `id`).
    INVESTMENT_PORTFOLIO: the symbols held in a portfolio (any owner), including ones no longer halal (needs `id`).
    WATCHLIST, WATCHLIST_GROUP: from the design, not available yet (refused at run time).

    Used by: FilterInput.type, engine.Evaluator._input_symbols, engine.check_filter_refs, routes.filter_actions.
    """

    UNIVERSE = "UNIVERSE"
    FILTER = "FILTER"
    FILTER_GROUP = "FILTER_GROUP"
    INVESTMENT_PORTFOLIO = "INVESTMENT_PORTFOLIO"
    WATCHLIST = "WATCHLIST"
    WATCHLIST_GROUP = "WATCHLIST_GROUP"


# Input types that exist in the app today; the rest arrive with watchlists.
# Used by: engine.Evaluator._input_symbols (refuses others), routes.filter_actions ("available" flag).
AVAILABLE_INPUTS = {InputType.UNIVERSE, InputType.FILTER, InputType.FILTER_GROUP, InputType.INVESTMENT_PORTFOLIO}


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

    Used by: Comparison (SymbolPriceAction, PriceChangeAction, UnrealizedPnlAction), routes.filter_actions.
    """

    LESS_THAN = "LESS_THAN"  # x < value
    BETWEEN = "BETWEEN"  # value <= x <= maxValue (inclusive)
    MORE_THAN = "MORE_THAN"  # x > value


class PriceReference(str, Enum):
    """Which price SYMBOL_PRICE compares.

    Used by: SymbolPriceAction.priceReference, engine.Evaluator.apply / _last_close, routes.filter_actions.
    """

    LAST_CLOSE = "LAST_CLOSE"  # close of the last completed trading day
    LATEST_BAR = "LATEST_BAR"  # newest price available (real-time minute bars, synced every minute)


class ValueType(str, Enum):
    """Whether PRICE_CHANGE / UNREALIZED_PNL compare in percent (3 = 3%) or in dollars.

    Used by: PriceChangeAction.valueType, UnrealizedPnlAction.valueType, engine.Evaluator.apply,
    routes.filter_actions.
    """

    PERCENT = "PERCENT"
    ABSOLUTE = "ABSOLUTE"


class TimePeriodUnit(str, Enum):
    """Unit of the PRICE_CHANGE look-back; sent unchanged to the market's GET /v1/price-change (its PeriodUnit).
    MINUTES is limited to MAX_MINUTE_LOOKBACK. Only market time counts: DAYS are trading days, MINUTES / HOURS
    market hours (04:00-20:00 New York); WEEKS / MONTHS / YEARS are calendar periods.

    Used by: PriceChangeAction.periodUnit, HoldingPeriodAction.periodUnit (calendar time there), routes.filter_actions.
    """

    MINUTES = "MINUTES"
    HOURS = "HOURS"
    DAYS = "DAYS"
    WEEKS = "WEEKS"
    MONTHS = "MONTHS"
    YEARS = "YEARS"


class FilterInput(BaseModel):
    """One input of a filter: {"type": "UNIVERSE"} or {"type": "FILTER" | "FILTER_GROUP" | "INVESTMENT_PORTFOLIO",
    "id": "..."}.

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

    Used by: SymbolPriceAction, PriceChangeAction, AverageVolumeAction, UnrealizedPnlAction, HoldingPeriodAction
    (base class); engine.Evaluator.apply (matches).
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


class ExcludeSymbolsAction(BaseModel):
    """Drop the listed tickers (e.g. one share class of a company listed twice, like GOOG next to GOOGL).
    Used by: FilterAction, engine.Evaluator.apply."""

    type: Literal["EXCLUDE_SYMBOLS"]
    tickers: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def upper(self):
        """Tickers are matched in upper case, without spaces."""
        self.tickers = [t.strip().upper() for t in self.tickers if t.strip()]
        if not self.tickers:
            raise ValueError("tickers is empty")
        return self


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


class MarketChangeAction(PriceChangeAction):
    """Market regime gate: keeps every symbol when the median percent price change of the whole universe over
    periodAmount x periodUnit matches the comparison, and none otherwise. The median of ~880 stocks is the "typical
    stock", so a few huge movers don't decide it.

    Example in a sell filter: {"periodAmount": 6, "periodUnit": "MONTHS", "condition": "LESS_THAN", "value": 0} =
    every held symbol passes while the typical stock is down over 6 months.

    Used by: FilterAction, engine.Evaluator.apply (via _market_change).
    """

    type: Literal["MARKET_CHANGE"]
    valueType: Literal[ValueType.PERCENT] = ValueType.PERCENT


class SectorRelativeChangeAction(PriceChangeAction):
    """Sector-relative strength: keeps symbols whose percent change over periodAmount x periodUnit minus the median
    change of their sector (all halal stocks of that sector) matches the comparison. +5 = 5 points better than the
    typical stock of its sector. Symbols without a sector are dropped.

    Used by: FilterAction, engine.Evaluator.apply (via _sector_relative).
    """

    type: Literal["SECTOR_RELATIVE_CHANGE"]
    valueType: Literal[ValueType.PERCENT] = ValueType.PERCENT


class Indicator(str, Enum):
    """Which indicator INDICATOR compares (market GET /v1/indicators: technical ones from completed daily bars,
    fundamentals and earnings dates from SEC filings as public on the day).

    Used by: IndicatorAction.indicator, engine (INDICATOR_FIELDS), routes.filter_actions.
    """

    VOLATILITY_20 = "VOLATILITY_20"  # annualised 20-day volatility, % (e.g. 35)
    ATR_14_PERCENT = "ATR_14_PERCENT"  # 14-day average true range as % of the close
    PRICE_VS_SMA_50 = "PRICE_VS_SMA_50"  # close vs 50-day average, % (> 0 = above it)
    PRICE_VS_SMA_200 = "PRICE_VS_SMA_200"  # close vs 200-day average, %
    FROM_52W_HIGH = "FROM_52W_HIGH"  # close vs 52-week high, % (0 at a new high, -20 = 20% below)
    ROE = "ROE"  # last annual net income / equity, % (SEC filings, counted 90 days after the year end)
    NET_MARGIN = "NET_MARGIN"  # last annual net income / revenue, %
    DEBT_TO_ASSETS = "DEBT_TO_ASSETS"  # (assets - equity) / assets, %, latest quarter end + 60 days
    DAYS_SINCE_EARNINGS = "DAYS_SINCE_EARNINGS"  # days since the last earnings release (8-K item 2.02)
    DAYS_TO_EARNINGS = "DAYS_TO_EARNINGS"  # days until the expected next one (last + 91 days; < 0 = overdue)


class IndicatorAction(Comparison):
    """Keep symbols whose indicator matches the comparison, e.g. PRICE_VS_SMA_200 MORE_THAN 0 (uptrend),
    FROM_52W_HIGH MORE_THAN -10 (near the high), VOLATILITY_20 LESS_THAN 60. Symbols without enough history are
    dropped.

    Used by: FilterAction, engine.Evaluator.apply (via MarketData.indicators).
    """

    type: Literal["INDICATOR"]
    indicator: Indicator


class VolumeType(str, Enum):
    """What AVERAGE_VOLUME compares: shares a day, or dollars a day (shares x price).

    Used by: AverageVolumeAction.volumeType, engine.Evaluator.apply, routes.filter_actions.
    """

    SHARES = "SHARES"
    DOLLARS = "DOLLARS"


class AverageVolumeAction(Comparison):
    """Keep symbols whose average daily trading over the last `days` completed trading days matches the
    comparison (days without any trade count as 0). Keeps thinly traded stocks, whose prices jump on a few shares
    and whose bid/ask are far apart, out of a filter. Symbols without enough history are dropped.

    Used by: FilterAction, engine.Evaluator.apply (via MarketData.volumes).
    """

    type: Literal["AVERAGE_VOLUME"]
    days: int = Field(default=20, ge=1, le=260)
    volumeType: VolumeType


class PortfolioIds(BaseModel):
    """`portfolioIds` of actions that look at portfolio positions: one or more portfolios, each at most once.

    Used by: PositionAction, UnrealizedPnlAction, HoldingPeriodAction (base class); engine.check_filter_refs
    (portfolios must exist),
    filters.store.portfolio_users (portfolio delete guard).
    """

    portfolioIds: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def check_unique(self):
        """Each portfolio at most once."""
        if len(set(self.portfolioIds)) != len(self.portfolioIds):
            raise ValueError("a portfolio is listed twice")
        return self


class PositionAction(PortfolioIds):
    """Keep symbols held (open position, any owner: manual or automation) in at least one of the portfolios
    (union of their positions). Used by: FilterAction, engine.Evaluator.apply.
    """

    type: Literal["POSITION"]


class UnrealizedPnlAction(PortfolioIds, Comparison):
    """Keep held symbols whose unrealized gain/loss matches the comparison: the shares of all the portfolios
    (any owner) together, valued at the IBKR live last trade (else the newest minute bar, like the portfolio
    page) against their cost. PERCENT is of the cost
    (-8 = down 8% from the buy price), ABSOLUTE in dollars. Symbols not held, or without a price, are dropped.

    Used by: FilterAction, engine.Evaluator.apply (via _positions).
    """

    type: Literal["UNREALIZED_PNL"]
    valueType: ValueType


class HoldingPeriodAction(PortfolioIds, Comparison):
    """Keep held symbols whose holding period, in `periodUnit`, matches the comparison: the time since the oldest open
    lot of the symbol in the portfolios (any owner) was bought. Calendar time: DAYS are 24 hours (a Friday buy is 3
    days old on Monday), MONTHS 30.4375 days, YEARS 365.25 days. Symbols not held there are dropped.

    Example: {"periodUnit": "DAYS", "condition": "MORE_THAN", "value": 5} = held for more than 5 days.

    Used by: FilterAction, engine.Evaluator.apply (via _opened).
    """

    type: Literal["HOLDING_PERIOD"]
    periodUnit: TimePeriodUnit

    @model_validator(mode="after")
    def check_not_negative(self):
        """A holding period can't be negative."""
        if self.value < 0:
            raise ValueError("value must be 0 or more")
        return self


# Any filter action; the "type" field picks the model. Used by: FilterIn.actions, engine.Evaluator.apply.
FilterAction = Annotated[
    Union[SymbolTypeAction, SectorAction, ExcludeSymbolsAction, SymbolPriceAction, PriceChangeAction, MarketChangeAction,
          SectorRelativeChangeAction, IndicatorAction,
          AverageVolumeAction, PositionAction, UnrealizedPnlAction, HoldingPeriodAction],
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
