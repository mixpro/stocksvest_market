"""Technical indicators from completed daily bars: one pure function shared by the market API and the backtester.

Used by: api (GET /v1/indicators), stocksvest_app.backtest.history.HistoricalMarket.indicators.
"""

from math import log, sqrt
from statistics import fmean, stdev

DAYS_NEEDED = 260  # daily bars to load: 52 weeks of highs plus a little. Used by: api.indicators, history


def compute(closes: list[float], highs: list[float], lows: list[float]) -> dict:
    """Indicators of one symbol, oldest bar first, completed trading days only. A value is None without enough bars.

    Returns:
        {close: last close, volatility20: annualised stdev of the last 20 daily log returns in %,
        atr14Percent: 14-day average true range as % of the close, sma50, sma200,
        priceVsSma50Percent / priceVsSma200Percent: close vs the average in %, high52w: highest high of the last 252
        bars, fromHigh52wPercent: close vs that high in % (0 at a new high, negative below it)}.
    """
    n = len(closes)
    close = closes[-1] if n else None
    out = {"close": close, "volatility20": None, "atr14Percent": None, "sma50": None, "sma200": None,
           "priceVsSma50Percent": None, "priceVsSma200Percent": None, "high52w": None, "fromHigh52wPercent": None}
    if not close:
        return out
    if n >= 21:
        rets = [log(closes[i] / closes[i - 1]) for i in range(n - 20, n) if closes[i - 1] > 0 and closes[i] > 0]
        if len(rets) >= 2:
            out["volatility20"] = stdev(rets) * sqrt(252) * 100
    if n >= 15:
        tr = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
              for i in range(n - 14, n)]
        out["atr14Percent"] = fmean(tr) / close * 100
    for days in (50, 200):
        if n >= days:
            sma = fmean(closes[-days:])
            out[f"sma{days}"] = sma
            out[f"priceVsSma{days}Percent"] = (close / sma - 1) * 100
    if n >= 252:
        high = max(highs[-252:])
        out["high52w"] = high
        out["fromHigh52wPercent"] = (close / high - 1) * 100 if high else None
    return out


if __name__ == "__main__":  # self-check
    c = [100.0 * 1.01 ** i for i in range(260)]
    r = compute(c, [x * 1.01 for x in c], [x * 0.99 for x in c])
    assert r["volatility20"] < 1e-6 and abs(r["fromHigh52wPercent"] + 0.99) < 0.01 and r["priceVsSma200Percent"] > 100
    assert compute([10.0], [10.0], [10.0])["volatility20"] is None
    print("ok")
