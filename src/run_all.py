"""Run the full pipeline in order: load data, rebuild views, run risk."""
import subprocess
import sys

STEPS = [
    ["src/load.py"],
    ["src/run_sql.py", "sql/02_performance.sql"],
    ["src/run_sql.py", "sql/03_attribution.sql"],
    ["src/risk.py"],
]

for step in STEPS:
    print(f"\n=== {' '.join(step)} ===")
    subprocess.run([sys.executable, *step], check=True)   # stops if any step fails

print("\nPipeline complete.")