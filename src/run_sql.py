"""Run a .sql file against the project database: python src\\run_sql.py sql\\02_performance.sql"""
import sys
from pathlib import Path

from db import get_engine

path = Path(sys.argv[1])
sql = path.read_text(encoding="utf-8")
with get_engine().begin() as conn:
    conn.exec_driver_sql(sql)
print(f"Ran {path}")