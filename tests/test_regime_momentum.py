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
