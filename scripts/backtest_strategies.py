"""Build the researched strategies (STRAT-1..3 from Kimi, STRAT-5 variants) in a fresh scratch app DB. Prints {strategy: {buy, sell}} ids as JSON.

Run from the repo root: DATA_DIR=<scratch dir> PYTHONPATH=tests uv run python scripts/backtest_strategies.py
then backtest each pair: python -m stocksvest_app.backtest --app-db <scratch dir>/app.db --buy <buy> --sell <sell> ...
"""

import json
import os

from fastapi.testclient import TestClient

from fake_broker import FakeBroker
from fake_market import FakeMarket
from stocksvest_app.api import create_app
from stocksvest_app.config import Settings

os.environ.setdefault("APP_API_KEYS", "k")
os.environ["APP_RUN_SCHEDULER"] = "false"
KEY = {"X-API-Key": os.environ["APP_API_KEYS"].split(",")[0]}


def pc(amount, unit, cond, value, hi=None):
    a = {"type": "PRICE_CHANGE", "periodAmount": amount, "periodUnit": unit, "valueType": "PERCENT",
         "condition": cond, "value": value}
    return {**a, "maxValue": hi} if hi is not None else a


def gate(min_price, min_dollars):
    return [{"type": "SYMBOL_PRICE", "priceReference": "LAST_CLOSE", "condition": "MORE_THAN", "value": min_price},
            {"type": "AVERAGE_VOLUME", "days": 20, "volumeType": "DOLLARS", "condition": "MORE_THAN",
             "value": min_dollars}]


def held(pf, days):
    return {"type": "HOLDING_PERIOD", "portfolioIds": [pf], "periodUnit": "DAYS", "condition": "MORE_THAN",
            "value": days}


def pnl(pf, below):
    return {"type": "UNREALIZED_PNL", "portfolioIds": [pf], "valueType": "PERCENT", "condition": "LESS_THAN",
            "value": below}


def market(months, cond, value=0):
    return {"type": "MARKET_CHANGE", "periodAmount": months, "periodUnit": "MONTHS", "condition": cond, "value": value}


# STRAT-5: liquid stocks ranked by 12-1 momentum, each buy 5% of equity (cash capped by maxPositionPercent), weekly.
def strat5(regime_months=None, stock_exit=True):
    regime = [market(regime_months, "MORE_THAN")] if regime_months else []
    sells = ([[market(regime_months, "LESS_THAN")]] if regime_months else [])
    return dict(buy=gate(10, 50e6) + regime + [pc(6, "MONTHS", "MORE_THAN", 0)],
                sells=lambda pf: sells + ([[held(pf, 30), pc(6, "MONTHS", "LESS_THAN", 0)]] if stock_exit else []),
                buy_schedule=MONDAY_11, top=20, cash=100000, pct=5, spread=0.3, cooldown=10080,
                rank="MOMENTUM_12_1", descending=True)


AT_11 = [{"type": "MARKET", "anchor": "REGULAR_OPEN", "offsetMinutes": 90}]
MONDAY_11 = [{"type": "WEEKLY", "onDays": ["MON"], "atTimes": ["11:00:00"]}]

STRATS = {
    "STRAT-1": dict(
        buy=gate(10, 50e6) + [pc(1, "YEARS", "MORE_THAN", 20), pc(6, "MONTHS", "MORE_THAN", 10),
                              pc(3, "MONTHS", "MORE_THAN", 0), pc(1, "MONTHS", "BETWEEN", -5, 12),
                              pc(1, "DAYS", "BETWEEN", -3, 2)],
        sells=lambda pf: [[held(pf, 30), pc(3, "MONTHS", "LESS_THAN", 0)],
                          [held(pf, 30), pc(6, "MONTHS", "LESS_THAN", 0)],
                          [pnl(pf, -15), pc(1, "MONTHS", "LESS_THAN", -8)]],
        buy_schedule=MONDAY_11, top=5, cash=500, pct=5, spread=0.3, cooldown=43200),
    "STRAT-2": dict(
        buy=gate(10, 50e6) + [pc(1, "YEARS", "MORE_THAN", 30), pc(6, "MONTHS", "MORE_THAN", 15),
                              pc(1, "MONTHS", "MORE_THAN", 5), pc(5, "DAYS", "BETWEEN", -8, -2),
                              pc(1, "DAYS", "BETWEEN", -4, 1)],
        sells=lambda pf: [[held(pf, 21)], [pc(3, "MONTHS", "LESS_THAN", 0)], [pnl(pf, -12)]],
        buy_schedule=AT_11, top=3, cash=500, pct=5, spread=0.3, cooldown=10080),
    "STRAT-3": dict(
        buy=gate(20, 100e6) + [pc(1, "DAYS", "BETWEEN", -5, -2), pc(5, "DAYS", "BETWEEN", -12, -4),
                               pc(1, "MONTHS", "BETWEEN", -15, 10)],
        sells=lambda pf: [[held(pf, 7)], [pnl(pf, -10)]],
        buy_schedule=AT_11, top=2, cash=250, pct=2.5, spread=0.2, cooldown=10080),
    "STRAT-5A": strat5(regime_months=6),  # regime 6 months + stock trend exit
    "STRAT-5B": strat5(),  # no regime (does the regime help?)
    "STRAT-5C": strat5(regime_months=3),  # faster regime
    "STRAT-5H": strat5(regime_months=6, stock_exit=False),  # hold, sell only when the market turns
    # RISK-3 Core 40: the 40 most-traded liquid stocks, equal weight; every 13 weeks sell all (10:30) and rebuy (11:00).
    "CORE-40": dict(
        buy=gate(10, 50e6), sells=lambda pf: [[]],
        buy_schedule=[{"type": "WEEKLY", "everyNWeeks": 13, "onDays": ["TUE"], "atTimes": ["11:00:00"],
                       "startAt": "2017-11-07T00:00:00-05:00"}],
        sell_schedule=[{"type": "WEEKLY", "everyNWeeks": 13, "onDays": ["TUE"], "atTimes": ["10:30:00"],
                        "startAt": "2017-11-07T00:00:00-05:00"}],
        top=40, cash=1000000, pct=2.5, spread=0.3, cooldown=None, rank="AVERAGE_DOLLAR_VOLUME_20", descending=True,
        sell_guards={"maxOrdersPerRun": 100}),  # sell the whole sleeve (the default is 10 orders a run)
}


def main():
    out = {}
    with TestClient(create_app(Settings(), market_factory=lambda s: FakeMarket(),
                               broker_factory=lambda a: FakeBroker(cash="100000"))) as c:
        def post(path, body):
            r = c.post(path, headers=KEY, json=body)
            assert r.status_code == 201, (path, r.text)
            return r.json()

        acct = post("/v1/broker-accounts", {"name": "Paper", "provider": "ALPACA"})
        for name, s in STRATS.items():
            pf = post("/v1/portfolios", {"name": name, "brokerAccountId": acct["id"], "capital": 10000})["id"]
            buy_f = post("/v1/filters", {"name": f"{name} buy", "inputs": [{"type": "UNIVERSE"}], "actions": s["buy"]})
            sell_fs = [post("/v1/filters", {"name": f"{name} sell {i + 1}",
                                            "inputs": [{"type": "INVESTMENT_PORTFOLIO", "id": pf}], "actions": acts})
                       for i, acts in enumerate(s["sells"](pf))]
            buy = post("/v1/automations", {
                "name": f"{name} Buy", "runTargets": [{"type": "FILTER", "id": buy_f["id"]}],
                "schedules": s["buy_schedule"],
                "actions": [{"type": "PLACE_ORDER", "side": "BUY",
                             "selection": {"mode": "TOP_N", "topN": s["top"], "rankBy": s.get("rank", "DAILY_CHANGE_PERCENT"),
                                           "descending": s.get("descending", False)},
                             "sizings": [{"mode": "CASH_AMOUNT", "value": s["cash"]}]}],
                "orderGuards": {"maxOrdersPerRun": s["top"], "maxPositionPercent": s["pct"],
                                "requireLivePrice": True, "maxSpreadPercent": s["spread"],
                                **({"rebuyCooldownMinutes": s["cooldown"]} if s["cooldown"] else {}),
                                "maxDataAgeMinutes": 25}})
            sell = post("/v1/automations", {
                "name": f"{name} Sell", "runTargets": [{"type": "FILTER", "id": f["id"]} for f in sell_fs],
                "schedules": s.get("sell_schedule", AT_11), "orderGuards": s.get("sell_guards", {}),
                "actions": [{"type": "PLACE_ORDER", "side": "SELL",
                             "sizings": [{"mode": "PERCENT_OF_POSITION", "value": 100}]}]})
            post(f"/v1/portfolios/{pf}/budgets", {"name": name, "amount": 10000,
                                                  "automationIds": [buy["id"], sell["id"]]})
            out[name] = {"buy": buy["id"], "sell": sell["id"]}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
