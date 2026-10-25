"""MARKET_CHANGE (market regime gate) and the momentum ranking keys."""

from decimal import Decimal

from stocksvest_app.automations import planner
from stocksvest_app.automations.models import PlaceOrderAction
from stocksvest_app.filters.engine import Evaluator
from stocksvest_app.filters.models import MarketChangeAction


class Market:
    """MarketData stand-in: a universe of three and changes by period."""

    FROM = {(1, "MONTHS"): {"A": 110, "B": 50, "C": None}, (12, "MONTHS"): {"A": 100, "B": 100, "C": 10}}

    def universe(self):
        return {"A": {}, "B": {}, "C": {}}

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
