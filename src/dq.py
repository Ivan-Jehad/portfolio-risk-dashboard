"""Data quality checks: write PASS/WARN/FAIL rows to dq_results; exit with an error on any FAIL."""
import logging
import sys
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import text

from db import get_engine

TOL = 1e-10   # tolerance for checks that should be exact (rounding noise only)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dq")


def scalar(conn, sql: str):
    return conn.execute(text(sql)).scalar()


def check_missing_prices(conn):
    no_price = scalar(conn, """
        SELECT COUNT(*) FROM v_prices_filled
        WHERE adj_close IS NULL
          AND ticker IN (SELECT ticker FROM holdings)
          AND price_date >= (SELECT MIN(start_date) FROM holdings)""")
    filled = pd.read_sql(text("""
        SELECT ticker, COUNT(*) AS days FROM v_prices_filled
        WHERE is_filled AND adj_close IS NOT NULL
          AND price_date >= (SELECT MIN(start_date) FROM holdings)
        GROUP BY ticker ORDER BY days DESC"""), conn)
    if no_price:
        return "FAIL", f"{no_price} holding-days have no price at all since the start date"
    if not filled.empty:
        top = ", ".join(f"{r.ticker} ({r.days})" for r in filled.head(5).itertuples())
        return "WARN", f"{int(filled['days'].sum())} stock-days carried forward from the prior price: {top}"
    return "PASS", "Every stock has a real price on every trading day since the start date"


def check_return_outliers(conn):
    df = pd.read_sql(text("""
        SELECT ticker, price_date, ret FROM v_stock_returns
        WHERE ABS(ret) > 0.25 ORDER BY ABS(ret) DESC"""), conn)
    if df.empty:
        return "PASS", "No daily stock return beyond +/-25% since 2019"
    top = ", ".join(f"{r.ticker} {r.price_date} {r.ret:+.0%}" for r in df.head(5).itertuples())
    return "WARN", f"{len(df)} daily returns beyond +/-25%, review for data errors: {top}"


def check_weights(conn):
    bench_file = scalar(conn, """
        SELECT SUM(weight) FROM benchmark_weights
        WHERE as_of_date = (SELECT MAX(as_of_date) FROM benchmark_weights)""")
    worst_daily = scalar(conn, """
        SELECT MAX(GREATEST(ABS(p - 1), ABS(b - 1))) FROM (
            SELECT price_date, SUM(port_weight) AS p, SUM(bench_weight) AS b
            FROM v_security_daily GROUP BY price_date) t""")
    worst = max(abs(bench_file - 1), worst_daily)
    status = "PASS" if worst < TOL else "FAIL"
    return status, f"Largest deviation of weights from 100%: {worst:.1e}"


def check_returns_reconcile(conn):
    worst = scalar(conn, """
        SELECT MAX(GREATEST(ABS(b.rp_total - p.port_return), ABS(b.rb_total - p.bench_return)))
        FROM (SELECT DISTINCT price_date, rp_total, rb_total FROM v_brinson_daily) b
        JOIN v_performance_daily p ON p.price_date = b.price_date""")
    status = "PASS" if worst < TOL else "FAIL"
    return status, f"Max daily gap between value-based and weight-based returns: {worst:.1e}"


def check_attribution_reconciles(conn):
    worst = scalar(conn, """
        SELECT MAX(ABS(d)) FROM (
            SELECT price_date, SUM(allocation + selection + interaction) - MAX(rp_total - rb_total) AS d
            FROM v_brinson_daily GROUP BY price_date) t""")
    status = "PASS" if worst < TOL else "FAIL"
    return status, f"Max daily gap between Brinson effects and active return: {worst:.1e}"


def check_stock_vs_sector(conn):
    gap = scalar(conn, """
        SELECT ABS((SELECT SUM(active_contribution) FROM v_contributions)
                 - (SELECT SUM(total_effect) FROM v_brinson_summary))""")
    status = "PASS" if gap < TOL else "FAIL"
    return status, f"Gap between stock-level and sector-level attribution totals: {gap:.1e}"


def check_benchmark_vs_xiu(conn):
    bench, xiu = conn.execute(text("SELECT bench_return, xiu_return FROM v_performance_summary")).one()
    te = scalar(conn, """
        SELECT STDDEV_SAMP(bench_return - xiu_return) * SQRT(252)
        FROM v_performance_daily WHERE port_return IS NOT NULL""")
    gap = abs(bench - xiu)
    status = "PASS" if gap < 0.01 else ("WARN" if gap < 0.03 else "FAIL")
    return status, (f"Benchmark {bench:.2%} vs XIU {xiu:.2%} over the period "
                    f"(gap {gap:.2%}), tracking error vs XIU {te:.2%}")


def check_var_backtest(conn):
    days, breaches = conn.execute(text(
        "SELECT COUNT(*), COALESCE(SUM(is_breach::int), 0) FROM var_backtest")).one()
    if days == 0:
        return "FAIL", "No backtest results found; run risk.py first"
    if breaches <= 4:
        status, zone = "PASS", "green"
    elif breaches <= 9:
        status, zone = "WARN", "yellow"
    else:
        status, zone = "FAIL", "red"
    return status, f"{breaches} breaches in {days} days (expected {0.01 * days:.1f}), Basel {zone} zone"


def check_freshness(conn):
    latest = scalar(conn, "SELECT MAX(price_date) FROM prices WHERE ticker = 'XIU'")
    gap = int(np.busday_count(latest, date.today()))
    status = "PASS" if gap <= 3 else "WARN"
    return status, f"Latest price date {latest} ({gap} business days ago)"


CHECKS = [
    ("Missing prices", check_missing_prices),
    ("Return outliers", check_return_outliers),
    ("Weights sum to 100%", check_weights),
    ("Returns reconcile", check_returns_reconcile),
    ("Attribution reconciles", check_attribution_reconciles),
    ("Stock vs sector attribution", check_stock_vs_sector),
    ("Benchmark vs XIU", check_benchmark_vs_xiu),
    ("VaR backtest", check_var_backtest),
    ("Data freshness", check_freshness),
]


def main():
    engine = get_engine()
    run_ts = datetime.now(timezone.utc)
    rows = []

    with engine.connect() as conn:
        for name, fn in CHECKS:
            try:
                status, detail = fn(conn)
            except Exception as e:   # a check that crashes counts as a failure
                conn.rollback()
                status, detail = "FAIL", f"Check could not run: {str(e)[:300]}"
            rows.append({"run_ts": run_ts, "check_name": name, "status": status, "detail": detail})
            level = logging.ERROR if status == "FAIL" else logging.WARNING if status == "WARN" else logging.INFO
            log.log(level, "%-28s %-4s  %s", name, status, detail)

    with engine.begin() as conn:
        pd.DataFrame(rows).to_sql("dq_results", conn, if_exists="append", index=False)
        # Latest run only, for the dashboard
        conn.execute(text("""
            CREATE OR REPLACE VIEW v_dq_latest AS
            SELECT check_name, status, detail, run_ts
            FROM dq_results
            WHERE run_ts = (SELECT MAX(run_ts) FROM dq_results)"""))

    fails = [r["check_name"] for r in rows if r["status"] == "FAIL"]
    if fails:
        log.error("Data quality FAILED: %s", ", ".join(fails))
        sys.exit(1)
    log.info("Data quality checks complete: no failures.")


if __name__ == "__main__":
    main()