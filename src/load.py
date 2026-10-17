import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf
from sqlalchemy import MetaData, Table, text
from sqlalchemy.dialects.postgresql import insert

from db import get_engine

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PRICE_START = "2019-01-01"   # early enough for the 2020 and 2022 stress windows

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("load")


def to_yahoo(ticker: str) -> str:
    """TSX ticker to Yahoo format: TECK.B -> TECK-B.TO, RY -> RY.TO."""
    return ticker.replace(".", "-") + ".TO"


def read_benchmark(path: Path):
    """Read the iShares XIU holdings file and return (as_of_date, clean equity rows)."""
    with open(path, encoding="utf-8-sig") as f:
        first_line = f.readline()   # Fund Holdings as of,"Oct 14, 2026"
    date_text = first_line.split(",", 1)[1].strip().strip('"')
    as_of = datetime.strptime(date_text, "%b %d, %Y").date()

    df = pd.read_csv(path, skiprows=2, thousands=",", encoding="utf-8-sig",
                     keep_default_na=False, na_values=[""])
    df = df[(df["Asset Class"] == "Equity") & (df["Weight (%)"] > 0)].copy()
    df["ticker"] = df["Ticker"].str.strip()
    df["yf_ticker"] = df["ticker"].map(to_yahoo)
    # re-scale to sum to 1
    df["weight"] = df["Weight (%)"] / df["Weight (%)"].sum()
    df = df.rename(columns={"Name": "name", "Sector": "sector"})

    log.info("Benchmark as of %s: %d stocks, weights re-scaled from %.2f%%",
             as_of, len(df), df["Weight (%)"].sum())
    return as_of, df[["ticker", "yf_ticker", "name", "sector", "weight"]]


def read_portfolio(path: Path, valid_tickers) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["start_date"],
                     keep_default_na=False, na_values=[""])
    df["ticker"] = df["ticker"].str.strip()

    unknown = set(df["ticker"]) - set(valid_tickers)
    if unknown:
        raise ValueError(
            f"Portfolio tickers not in the benchmark file: {sorted(unknown)}")
    if df["ticker"].duplicated().any():
        raise ValueError("Portfolio has duplicate tickers")

    df["start_date"] = df["start_date"].dt.date
    log.info("Portfolio: %d holdings", len(df))
    return df[["ticker", "shares", "start_date"]]


def download_prices(yf_tickers: list[str]) -> pd.DataFrame:
    log.info("Downloading prices for %d tickers from %s...",
             len(yf_tickers), PRICE_START)
    raw = yf.download(yf_tickers, start=PRICE_START, auto_adjust=False,
                      actions=False, progress=False, threads=True)

    def to_long(field: str, name: str) -> pd.DataFrame:
        return raw[field].reset_index().melt(id_vars="Date", var_name="yf_ticker", value_name=name)

    df = to_long("Adj Close", "adj_close").merge(
        to_long("Close", "close"), on=["Date", "yf_ticker"])
    df = df.dropna(subset=["adj_close"]).rename(columns={"Date": "price_date"})
    df["price_date"] = pd.to_datetime(df["price_date"]).dt.date

    missing = set(yf_tickers) - set(df["yf_ticker"])
    if missing:
        log.warning("No price data returned for: %s", sorted(missing))
    log.info("Downloaded %d price rows", len(df))
    return df


def upsert(conn, table_name: str, df: pd.DataFrame, key_cols: list[str], chunk: int = 5000):
    """Insert rows; if the key already exists, update the other columns instead."""
    if df.empty:
        return
    table = Table(table_name, MetaData(), autoload_with=conn)
    clean = df.astype(object).where(df.notna(), None)   # NaN -> NULL
    records = clean.to_dict(orient="records")
    for i in range(0, len(records), chunk):
        stmt = insert(table).values(records[i:i + chunk])
        update_cols = {c: stmt.excluded[c]
                       for c in df.columns if c not in key_cols}
        if update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=key_cols, set_=update_cols)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=key_cols)
        conn.execute(stmt)
    log.info("Upserted %d rows into %s", len(records), table_name)


def main():
    as_of, bench = read_benchmark(DATA / "xiu_holdings.csv")
    holdings = read_portfolio(DATA / "portfolio.csv", bench["ticker"])

    etf = pd.DataFrame([{"ticker": "XIU", "yf_ticker": "XIU.TO",
                         "name": "iShares S&P/TSX 60 Index ETF", "sector": "ETF"}])
    securities = pd.concat([
        bench[["ticker", "yf_ticker", "name", "sector"]].assign(
            security_type="Equity"),
        etf.assign(security_type="ETF"),
    ], ignore_index=True)

    prices = download_prices(securities["yf_ticker"].tolist())
    prices = prices.merge(securities[["ticker", "yf_ticker"]], on="yf_ticker")
    prices = prices[["ticker", "price_date", "close", "adj_close"]]

    engine = get_engine()
    with engine.begin() as conn:   # one transaction: all or nothing
        upsert(conn, "securities", securities, ["ticker"])

        # Weights and holdings are full snapshots, so replace them rather than merge
        conn.execute(
            text("DELETE FROM benchmark_weights WHERE as_of_date = :d"), {"d": as_of})
        upsert(conn, "benchmark_weights",
               bench[["ticker", "weight"]].assign(as_of_date=as_of), ["as_of_date", "ticker"])
        conn.execute(text("DELETE FROM holdings"))
        upsert(conn, "holdings", holdings, ["ticker"])

        upsert(conn, "prices", prices, ["ticker", "price_date"])

        if conn.execute(text("SELECT to_regclass('v_prices_filled')")).scalar():
            conn.execute(text("REFRESH MATERIALIZED VIEW v_prices_filled"))
            log.info("Refreshed v_prices_filled")

    # Warn if any holding has no price on its start date
    start_prices = prices.merge(holdings, left_on=["ticker", "price_date"],
                                right_on=["ticker", "start_date"])
    no_start = set(holdings["ticker"]) - set(start_prices["ticker"])
    if no_start:
        log.warning(
            "No price on start date for: %s (is it a trading day?)", sorted(no_start))

    log.info("Load complete.")


if __name__ == "__main__":
    main()
