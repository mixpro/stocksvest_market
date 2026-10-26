"""MARKET_CHANGE (market regime gate), the momentum ranking keys and the sector cap."""

from decimal import Decimal

from fake_broker import FakeBroker

from stocksvest_app.automations import planner
from stocksvest_app.automations.models import OrderGuards, PlaceOrderAction
from stocksvest_app.filters.engine import Evaluator
from stocksvest_app.filters.models import MarketChangeAction


class Market:
    """MarketData stand-in: a universe of three and changes by period."""

    FROM = {(1, "MONTHS"): {"A": 110, "B": 50, "C": None}, (12, "MONTHS"): {"A": 100, "B": 100, "C": 10}}

    def universe(self):
        return {"A": {}, "B": {}, "C": {}}

    def volumes(self, tickers, days):
        return {t: {"averageDollarVolume": v} for t, v in {"A": 5e8, "B": 9e8, "C": None}.items() if t in tickers}

    def price_changes(self, tickers, amount, unit):
        if (amount, unit) == (6, "MONTHS"):  # A +10%, B -20%, C +1%: median +1%
            return {t: {"changePercent": v} for t, v in {"A": 10, "B": -20, "C": 1}.items() if t in tickers}
        return {t: {"fromPrice": self.FROM[(amount, unit)][t]} for t in tickers}


def test_market_change_keeps_all_or_nothing():
    ev = Evaluator(None, Market())
    up = MarketChangeAction(type="MARKET_CHANGE", periodAmount=6, periodUnit="MONTHS", condition="MORE_THAN", value=0)
    down = up.model_copy(update={"condition": "LESS_THAN"})
    assert ev.apply(up, {"A", "B"}) == {"A", "B"}  # median of all three (+1%), not just of the candidates
    assert ev.apply(down, {"A", "B"}) == set()


def test_momentum_ranks_12_months_to_1_month_ago():
    action = PlaceOrderAction.model_validate({
        "type": "PLACE_ORDER", "side": "BUY", "sizings": [{"mode": "CASH_AMOUNT", "value": 100}],
        "selection": {"mode": "TOP_N", "topN": 2, "rankBy": "MOMENTUM_12_1"}})
    snaps = {t: {"latestPrice": 1} for t in "ABC"}
    planner.add_rank_data(action, ["A", "B", "C"], snaps, Market())
    assert round(snaps["A"]["momentum12_1"], 6) == 10 and snaps["B"]["momentum12_1"] == -50
    assert snaps["C"]["momentum12_1"] is None
    ctx = planner.PlanContext(snapshots=snaps, holdings={}, open_buys=set(), open_sells={},
                              budget_available=Decimal(1000), budget_equity=Decimal(1000), session="REGULAR",
                              data_age_minutes=0)
    assert planner.select(action, ["A", "B", "C"], ctx, None)["symbols"] == ["A", "B"]  # C (no data) ranks last


def test_sector_cap_skips_full_sectors_before_the_top_n():
    action = PlaceOrderAction.model_validate({
        "type": "PLACE_ORDER", "side": "BUY", "sizings": [{"mode": "CASH_AMOUNT", "value": 100000}],
        "selection": {"mode": "TOP_N", "topN": 3, "rankBy": "LAST_PRICE"}})
    guards = OrderGuards(maxPositionPercent=10, maxSectorPercent=25, maxOrdersPerRun=10)
    prices = {"T1": 50, "T2": 40, "T3": 30, "T4": 20, "E1": 10}  # ranked by price: T1 > T2 > T3 > T4 > E1
    snaps = {t: {"latestPrice": p, "priceSource": "ALPACA", "bid": p, "ask": p} for t, p in prices.items()}
    snaps["H"] = {"latestPrice": 10}
    ctx = planner.PlanContext(snapshots=snaps, holdings={"H": planner.Holding(Decimal(50), Decimal(500))},
                              open_buys=set(), open_sells={}, budget_available=Decimal(9500),
                              budget_equity=Decimal(10000), session="REGULAR", data_age_minutes=0,
                              sectors={"T1": "TECH", "T2": "TECH", "T3": "TECH", "T4": "TECH", "E1": "ENERGY",
                                       "H": "TECH"})
    chosen = planner.select(action, list(prices), ctx, guards)
    # TECH holds $500; a cap of $2,500 fits two $1,000 buys (T1, T2), then T3 and T4 are left out for E1.
    assert chosen["symbols"] == ["T1", "T2", "E1"] and chosen["sectorFull"] == ["T3", "T4"]
    intents = planner.size(action, guards, chosen["symbols"], ctx, FakeBroker())
    assert [(i["ticker"], i["decision"], i["quantity"]) for i in intents] == [
        ("T1", "PLACE", 20), ("T2", "PLACE", 25), ("E1", "PLACE", 100)]
    ctx.sector_planned["TECH"] += Decimal(900)  # e.g. an earlier action of the run bought more TECH
    assert planner.size(action, guards, ["T3"], ctx, FakeBroker())[0]["skipReason"] == "SECTOR_FULL"


def test_rank_by_20_day_dollar_volume():
    action = PlaceOrderAction.model_validate({
        "type": "PLACE_ORDER", "side": "BUY", "sizings": [{"mode": "CASH_AMOUNT", "value": 100}],
        "selection": {"mode": "TOP_N", "topN": 3, "rankBy": "AVERAGE_DOLLAR_VOLUME_20"}})
    snaps = {t: {"latestPrice": 1} for t in "ABC"}
    planner.add_rank_data(action, ["A", "B", "C"], snaps, Market())
    ctx = planner.PlanContext(snapshots=snaps, holdings={}, open_buys=set(), open_sells={},
                              budget_available=Decimal(1000), budget_equity=Decimal(1000), session="REGULAR",
                              data_age_minutes=0)
    assert planner.select(action, ["A", "B", "C"], ctx, None)["symbols"] == ["B", "A", "C"]


def test_volatility_scaled_buys_and_max_open_positions():
    """targetVolatilityPercent 40, 5% positions of $10k: 20% vol -> x1.5 ($750), 80% -> x0.5 ($250), 40% -> x1."""
    class IndMarket(Market):
        def indicators(self, tickers):
            return {t: {"volatility20": v, "fromHigh52wPercent": -v} for t, v in
                    {"A": 20, "B": 80, "C": 40}.items() if t in tickers}

    action = PlaceOrderAction.model_validate({
        "type": "PLACE_ORDER", "side": "BUY", "sizings": [{"mode": "CASH_AMOUNT", "value": 100000}],
        "selection": {"mode": "TOP_N", "topN": 3, "rankBy": "VOLATILITY_20", "descending": False}})
    guards = OrderGuards(maxPositionPercent=5, targetVolatilityPercent=40, maxOpenPositions=3)
    snaps = {t: {"latestPrice": 10, "priceSource": "ALPACA", "bid": 10, "ask": 10} for t in "ABC"}
    planner.add_rank_data(action, ["A", "B", "C"], snaps, IndMarket(), guards)
    ctx = planner.PlanContext(snapshots=snaps, holdings={"H": planner.Holding(Decimal(1), Decimal(10))},
                              open_buys=set(), open_sells={}, budget_available=Decimal(10000),
                              budget_equity=Decimal(10000), session="REGULAR", data_age_minutes=0)
    chosen = planner.select(action, ["A", "B", "C"], ctx, guards)
    assert chosen["symbols"] == ["A", "C", "B"]  # calmest first
    intents = planner.size(action, guards, chosen["symbols"], ctx, FakeBroker())
    assert [(i["ticker"], i["quantity"]) for i in intents[:2]] == [("A", 75), ("C", 50)]
    assert intents[2]["skipReason"] == "MAX_POSITIONS"  # H + A + C = 3 symbols


def test_max_correlation_skips_stocks_that_move_with_a_holding_or_an_earlier_pick():
    wave = [0.01 * ((i % 7) - 3) for i in range(119)]
    other = [0.01 * ((i % 5) - 2) for i in range(119)]

    class RetMarket(Market):
        def returns(self, tickers, days):
            series = {"H": wave, "A": [x * 2 for x in wave], "B": other, "C": [x + 0.001 for x in other], "D": wave[::-1]}
            return {t: series[t] for t in tickers if t in series}

    action = PlaceOrderAction.model_validate({
        "type": "PLACE_ORDER", "side": "BUY", "sizings": [{"mode": "CASH_AMOUNT", "value": 100}],
        "selection": {"mode": "TOP_N", "topN": 2, "rankBy": "LAST_PRICE"}})
    guards = OrderGuards(maxCorrelation=0.7)
    snaps = {t: {"latestPrice": p} for t, p in {"A": 4, "B": 3, "C": 2, "D": 1}.items()}
    holdings = {"H": planner.Holding(Decimal(1), Decimal(1))}
    planner.add_rank_data(action, list("ABCD"), snaps, RetMarket(), guards, held=holdings)
    ctx = planner.PlanContext(snapshots=snaps, holdings=holdings, open_buys=set(), open_sells={},
                              budget_available=Decimal(1000), budget_equity=Decimal(1000), session="REGULAR",
                              data_age_minutes=0)
    chosen = planner.select(action, list("ABCD"), ctx, guards)
    # A mirrors the holding H, C mirrors the earlier pick B; D is H backwards (uncorrelated enough).
    assert chosen["symbols"] == ["B", "D"] and chosen["tooCorrelated"] == ["A", "C"]


def test_fractional_shares_buy_expensive_stocks_and_sell_whole():
    """5% of $9,000 = $450: a $1,200 stock is 0 whole shares, 0.375 with fractionalShares; selling all sells 0.375."""
    buy = PlaceOrderAction.model_validate({
        "type": "PLACE_ORDER", "side": "BUY", "sizings": [{"mode": "CASH_AMOUNT", "value": 100000}],
        "selection": {"mode": "TOP_N", "topN": 1, "rankBy": "LAST_PRICE"}})
    snaps = {"X": {"latestPrice": 1200, "priceSource": "ALPACA", "bid": 1200, "ask": 1200}}

    def ctx(holdings):
        return planner.PlanContext(snapshots=snaps, holdings=holdings, open_buys=set(), open_sells={},
                                   budget_available=Decimal(9000), budget_equity=Decimal(9000), session="REGULAR",
                                   data_age_minutes=0)

    whole = planner.size(buy, OrderGuards(maxPositionPercent=5), ["X"], ctx({}), FakeBroker())[0]
    assert whole["skipReason"] == "MAX_POSITION"
    frac = planner.size(buy, OrderGuards(maxPositionPercent=5, fractionalShares=True), ["X"], ctx({}), FakeBroker())[0]
    assert frac["decision"] == "PLACE" and frac["quantity"] == 0.375
    limit = buy.model_copy(update={"orderType": "LIMIT", "limitOffsetPercent": Decimal(1)})
    assert planner.size(limit, OrderGuards(maxPositionPercent=5, fractionalShares=True), ["X"], ctx({}),
                        FakeBroker())[0]["decision"] == "SKIP"  # fractions are MARKET + DAY only
    sell = PlaceOrderAction.model_validate({"type": "PLACE_ORDER", "side": "SELL",
                                            "sizings": [{"mode": "PERCENT_OF_POSITION", "value": 100}]})
    held = {"X": planner.Holding(Decimal("0.375"), Decimal(450))}
    out = planner.size(sell, OrderGuards(), ["X"], ctx(held), FakeBroker())[0]
    assert out["decision"] == "PLACE" and out["quantity"] == 0.375  # no fractionalShares needed to sell it all
