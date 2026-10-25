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
def strat5(regime_months=None, stock_exit=True, extra=(), exits=None, guards=None, rank="MOMENTUM_12_1"):
    """extra: more buy filter steps; exits: pf -> sell filters replacing the stock exit; guards: more buy guards."""
    regime = [market(regime_months, "MORE_THAN")] if regime_months else []
    sells = ([[market(regime_months, "LESS_THAN")]] if regime_months else [])
    stock = exits or (lambda pf: [[held(pf, 30), pc(6, "MONTHS", "LESS_THAN", 0)]] if stock_exit else [])
    return dict(buy=gate(10, 50e6) + regime + [pc(6, "MONTHS", "MORE_THAN", 0)] + list(extra),
                sells=lambda pf: sells + stock(pf),
                buy_schedule=MONDAY_11, top=20, cash=100000, pct=5, spread=0.3, cooldown=10080,
                rank=rank, descending=True, guards=guards or {})


def ind(indicator, cond, value):
    return {"type": "INDICATOR", "indicator": indicator, "condition": cond, "value": value}


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
    # 2026-10-25 overnight batch. RISK-2: volatility-scaled sizing.
    "5B-VOL40": strat5(guards={"targetVolatilityPercent": 40}),
    "5B-VOL60": strat5(guards={"targetVolatilityPercent": 60}),
    # RISK-4: faster stock exits.
    "5B-X-NOMIN": strat5(exits=lambda pf: [[pc(6, "MONTHS", "LESS_THAN", 0)]]),
    "5B-X-3M": strat5(exits=lambda pf: [[held(pf, 30), pc(3, "MONTHS", "LESS_THAN", 0)]]),
    "5B-X-3OR6": strat5(exits=lambda pf: [[held(pf, 30), pc(3, "MONTHS", "LESS_THAN", 0)],
                                          [held(pf, 30), pc(6, "MONTHS", "LESS_THAN", 0)]]),
    "5B-X-SMA200": strat5(exits=lambda pf: [[held(pf, 30), ind("PRICE_VS_SMA_200", "LESS_THAN", 0)]]),
    # FEAT-4: trend entries; 52-week-high momentum (George & Hwang 2004).
    "5B-T-SMA200": strat5(extra=[ind("PRICE_VS_SMA_200", "MORE_THAN", 0)]),
    "5B-T-HIGH15": strat5(extra=[ind("FROM_52W_HIGH", "MORE_THAN", -15)]),
    "5B-R-HIGH": strat5(rank="FROM_52W_HIGH"),
    # FEAT-5: sector-relative strength; FEAT-2: volatility cap; FEAT-3: position count.
    "5B-SREL6": strat5(extra=[{"type": "SECTOR_RELATIVE_CHANGE", "periodAmount": 6, "periodUnit": "MONTHS",
                               "condition": "MORE_THAN", "value": 0}]),
    "5B-LOWVOL60": strat5(extra=[ind("VOLATILITY_20", "LESS_THAN", 60)]),
    "5B-MAXPOS20": strat5(guards={"maxOpenPositions": 20}),
    # FEAT-9: correlation limit; combinations of the best of the batch.
    "5B-CORR70": strat5(guards={"maxCorrelation": 0.7}),
    "5B-CORR50": strat5(guards={"maxCorrelation": 0.5}),
    "5B-VOL60-CORR70": strat5(guards={"targetVolatilityPercent": 60, "maxCorrelation": 0.7}),
    # FEAT-8 quality, FEAT-6 earnings, BT-4 trailing exit.
    "5B-Q-ROE10": strat5(extra=[ind("ROE", "MORE_THAN", 10)]),
    "5B-Q-PROFIT": strat5(extra=[ind("NET_MARGIN", "MORE_THAN", 0)]),
    "5B-E-SKIP7": strat5(extra=[ind("DAYS_TO_EARNINGS", "MORE_THAN", 7)]),
    "5B-E-AFTER": strat5(extra=[ind("DAYS_SINCE_EARNINGS", "LESS_THAN", 30)]),  # post-earnings drift
    "5B-X-TRAIL25": strat5(exits=lambda pf: [[held(pf, 30), pc(6, "MONTHS", "LESS_THAN", 0)],
                                             [held(pf, 30), ind("FROM_52W_HIGH", "LESS_THAN", -25)]]),
    "5B-VOL60-NOMIN": strat5(guards={"targetVolatilityPercent": 60}, exits=lambda pf: [[pc(6, "MONTHS", "LESS_THAN", 0)]]),
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
                               broker_factory=lambda a: FakeBroker(cash="10000000"))) as c:
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
                                "maxDataAgeMinutes": 25, **s.get("guards", {})}})
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
