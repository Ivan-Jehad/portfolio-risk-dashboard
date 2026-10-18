"""Export daily series to CSV for the LaTeX commentary (docs/latex/)."""
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from db import get_engine

OUT = Path(__file__).resolve().parents[1] / "docs" / "latex"
OUT.mkdir(parents=True, exist_ok=True)

QUERIES = {
    "performance_daily.csv": """
        SELECT price_date              AS date,
               100 * port_cum_return   AS portfolio,
               100 * bench_cum_return  AS benchmark,
               100 * drawdown          AS drawdown
        FROM v_performance_daily ORDER BY price_date""",
    "var_backtest.csv": """
        SELECT test_date                AS date,
               actual_pnl / 1000.0      AS pnl,
               -var_amount / 1000.0     AS var,
               CASE WHEN is_breach THEN 1 ELSE 0 END AS breach
        FROM var_backtest ORDER BY test_date""",
}

with get_engine().connect() as conn:
    for name, sql in QUERIES.items():
        df = pd.read_sql(text(sql), conn)
        df.to_csv(OUT / name, index=False, float_format="%.4f")
        print(f"Wrote {OUT / name} ({len(df)} rows)")