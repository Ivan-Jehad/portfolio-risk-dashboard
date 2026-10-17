"""Risk engine: historical and parametric VaR, expected shortfall, VaR backtest, stress tests."""
import logging
import math
from statistics import NormalDist

import numpy as np
import pandas as pd
from sqlalchemy import text

from db import get_engine

CONFIDENCE = 0.99     # 99% one-day VaR
# trading days of history behind each VaR estimate (~2 years)
WINDOW = 500
BACKTEST_DAYS = 250   # number of past days to backtest (~1 year)

# Search ranges for historical stress windows. The exact peak and trough
# are detected from XIU's prices, so no dates are hard-coded or guessed.
STRESS_PERIODS = {
    "COVID crash 2020": ("2020-01-01", "2020-06-30"),
    "2022 selloff": ("2022-01-01", "2022-12-31"),
}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("risk")


def load_data(conn):
    """Gap-filled prices (dates x tickers), share counts, current benchmark weights, XIU prices."""
    prices = pd.read_sql(
        text("SELECT price_date, ticker, adj_close FROM v_prices_filled ORDER BY price_date"),
        conn, parse_dates=["price_date"],
    ).pivot(index="price_date", columns="ticker", values="adj_close")

    shares = pd.read_sql(
        text("SELECT ticker, shares FROM holdings"), conn).set_index("ticker")["shares"]

    bench_w = pd.read_sql(
        text("SELECT ticker, bench_weight FROM v_holdings_current"), conn
    ).set_index("ticker")["bench_weight"]

    xiu = pd.read_sql(
        text("SELECT price_date, adj_close FROM prices WHERE ticker = 'XIU' ORDER BY price_date"),
        conn, parse_dates=["price_date"],
    ).set_index("price_date")["adj_close"]

    return prices, shares, bench_w, xiu


def historical_var_es(pnl: np.ndarray, conf: float = CONFIDENCE):
    """VaR = loss at the (1 - conf) percentile; ES = average loss at or beyond it. Both positive."""
    var = -np.quantile(pnl, 1 - conf)
    es = -pnl[pnl <= -var].mean()
    return float(var), float(es)


def parametric_var(window: pd.DataFrame, values: pd.Series, conf: float = CONFIDENCE) -> float:
    """Normal-distribution VaR: z-score x dollar volatility from the covariance matrix."""
    cov = window[values.index].cov().to_numpy()
    v = values.to_numpy()
    sigma = math.sqrt(v @ cov @ v)
    return NormalDist().inv_cdf(conf) * sigma


def basel_zone(breaches: int) -> str:
    """Basel traffic light for 250 days of 99% VaR."""
    if breaches <= 4:
        return "green"
    return "yellow" if breaches <= 9 else "red"


def kupiec_pof(breaches: int, n: int, conf: float = CONFIDENCE):
    """Kupiec proportion-of-failures test. Returns (LR statistic, p-value)."""
    p, x = 1 - conf, breaches
    phat = x / n
    ll_null = (n - x) * math.log(1 - p) + x * math.log(p)
    ll_alt = ((n - x) * math.log(1 - phat) if phat < 1 else 0.0) + \
        (x * math.log(phat) if x > 0 else 0.0)
    lr = -2 * (ll_null - ll_alt)
    # chi-squared with 1 degree of freedom
    p_value = math.erfc(math.sqrt(lr / 2))
    return lr, p_value


def drawdown_window(series: pd.Series, lo: str, hi: str):
    """Peak and trough dates of the largest decline within [lo, hi]."""
    s = series.loc[lo:hi]
    trough = (s / s.cummax() - 1).idxmin()
    peak = s.loc[:trough].idxmax()
    return peak, trough


def main():
    engine = get_engine()
    with engine.connect() as conn:
        prices, shares, bench_w, xiu = load_data(conn)

    held = shares.index.tolist()
    P = prices[held]
    R = P / P.shift(1) - 1                      # daily returns of held stocks
    as_of = P.index[-1]
    # today's market value per holding
    values = shares * P.iloc[-1]
    port_value = float(values.sum())

    # ---------- Today's VaR and expected shortfall ----------
    window = R.iloc[-WINDOW:]
    if window.isna().any().any():
        bad = window.columns[window.isna().any()].tolist()
        raise ValueError(f"Missing returns in the VaR window for: {bad}")

    pnl = window.to_numpy() @ values.to_numpy()   # 500 hypothetical one-day P&Ls
    h_var, h_es = historical_var_es(pnl)
    p_var = parametric_var(window, values)

    log.info("As of %s, portfolio value $%s", as_of.date(), f"{port_value:,.0f}")
    log.info("Historical 99%% VaR $%s (%.2f%%), ES $%s (%.2f%%)",
             f"{h_var:,.0f}", 100 * h_var / port_value, f"{h_es:,.0f}", 100 * h_es / port_value)
    log.info("Parametric 99%% VaR $%s (%.2f%%)", f"{p_var:,.0f}", 100 * p_var / port_value)

    var_rows = pd.DataFrame([
        {"as_of_date": as_of.date(), "method": "historical", "confidence": CONFIDENCE,
         "portfolio_value": port_value, "var_amount": h_var, "es_amount": h_es},
        {"as_of_date": as_of.date(), "method": "parametric", "confidence": CONFIDENCE,
         "portfolio_value": port_value, "var_amount": p_var, "es_amount": None},
    ])

    # ---------- Backtest: each day's VaR uses only the 500 days before it ----------
    n = len(R)
    if n - BACKTEST_DAYS - WINDOW < 1:
        raise ValueError("Not enough price history for the backtest")

    bt_rows = []
    for k in range(n - BACKTEST_DAYS, n):
        # holdings value at yesterday's close
        v_prev = shares * P.iloc[k - 1]
        hist_pnl = R.iloc[k - WINDOW:k].to_numpy() @ v_prev.to_numpy()
        var_k, _ = historical_var_es(hist_pnl)
        actual = float(R.iloc[k].to_numpy() @ v_prev.to_numpy())
        bt_rows.append({"test_date": R.index[k].date(), "var_amount": var_k,
                        "actual_pnl": actual, "is_breach": bool(-actual > var_k)})
    bt = pd.DataFrame(bt_rows)

    breaches = int(bt["is_breach"].sum())
    lr, p_value = kupiec_pof(breaches, len(bt))
    log.info("Backtest: %d breaches in %d days (expected %.1f), Basel zone %s, Kupiec p-value %.3f",
             breaches, len(bt), (1 - CONFIDENCE) * len(bt), basel_zone(breaches), p_value)

    # ---------- Historical stress tests on today's holdings ----------
    stress_rows = []
    for name, (lo, hi) in STRESS_PERIODS.items():
        start, end = drawdown_window(xiu, lo, hi)
        stock_ret = prices.loc[end] / prices.loc[start] - 1

        missing = [t for t in held if pd.isna(stock_ret.get(t))]
        if missing:
            log.warning(
                "%s: no prices for %s; their return is set to 0", name, missing)
        stock_ret = stock_ret.fillna(0.0)

        port_pnl = float((values * stock_ret[held]).sum())
        bench_ret = float(
            (bench_w * stock_ret.reindex(bench_w.index).fillna(0.0)).sum())
        stress_rows.append({"scenario": name, "window_start": start.date(), "window_end": end.date(),
                            "portfolio_pnl": port_pnl, "portfolio_return": port_pnl / port_value,
                            "benchmark_return": bench_ret})
        log.info("%s (%s to %s): portfolio %.1f%% ($%s), benchmark %.1f%%",
                 name, start.date(), end.date(), 100 * port_pnl / port_value,
                 f"{port_pnl:,.0f}", 100 * bench_ret)

    # ---------- Write results (one transaction) ----------
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM var_results WHERE as_of_date = :d"), {
                     "d": as_of.date()})
        var_rows.to_sql("var_results", conn, if_exists="append", index=False)
        conn.execute(text("DELETE FROM var_backtest"))
        bt.to_sql("var_backtest", conn, if_exists="append", index=False)
        conn.execute(text("DELETE FROM stress_results"))
        pd.DataFrame(stress_rows).to_sql("stress_results",
                                         conn, if_exists="append", index=False)

    log.info("Risk results written.")


if __name__ == "__main__":
    main()
