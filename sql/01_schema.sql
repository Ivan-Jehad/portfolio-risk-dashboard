DROP TABLE IF EXISTS dq_results, stress_results, var_backtest, var_results,
    holdings, benchmark_weights, prices, securities CASCADE;

-- Every stock in the TSX 60, plus the XIU ETF for sanity checks
CREATE TABLE securities (
    ticker         TEXT PRIMARY KEY,           -- as in the XIU file, e.g. TECK.B
    yf_ticker      TEXT NOT NULL UNIQUE,       -- Yahoo format, e.g. TECK-B.TO
    name           TEXT,
    sector         TEXT NOT NULL,
    security_type  TEXT NOT NULL DEFAULT 'Equity'   -- 'Equity' or 'ETF'
);

-- Daily prices, one row per stock per trading day
CREATE TABLE prices (
    ticker      TEXT NOT NULL REFERENCES securities(ticker),
    price_date  DATE NOT NULL,
    close       DOUBLE PRECISION,
    adj_close   DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (ticker, price_date)
);
CREATE INDEX idx_prices_date ON prices (price_date);

-- TSX 60 weights from the XIU holdings file (re-scaled to sum to 1)
CREATE TABLE benchmark_weights (
    as_of_date  DATE NOT NULL,
    ticker      TEXT NOT NULL REFERENCES securities(ticker),
    weight      DOUBLE PRECISION NOT NULL CHECK (weight > 0 AND weight <= 1),
    PRIMARY KEY (as_of_date, ticker)
);

-- Your portfolio from portfolio.csv
CREATE TABLE holdings (
    ticker      TEXT PRIMARY KEY REFERENCES securities(ticker),
    shares      DOUBLE PRECISION NOT NULL CHECK (shares > 0),
    start_date  DATE NOT NULL
);

-- VaR and expected shortfall, written by risk.py
CREATE TABLE var_results (
    as_of_date       DATE NOT NULL,
    method           TEXT NOT NULL CHECK (method IN ('historical', 'parametric')),
    confidence       DOUBLE PRECISION NOT NULL,     -- e.g. 0.99
    portfolio_value  DOUBLE PRECISION NOT NULL,
    var_amount       DOUBLE PRECISION NOT NULL,     -- positive number = loss
    es_amount        DOUBLE PRECISION,              -- historical method only
    PRIMARY KEY (as_of_date, method, confidence)
);

-- One row per backtest day: predicted VaR vs. what actually happened
CREATE TABLE var_backtest (
    test_date   DATE PRIMARY KEY,
    var_amount  DOUBLE PRECISION NOT NULL,
    actual_pnl  DOUBLE PRECISION NOT NULL,
    is_breach   BOOLEAN NOT NULL
);

-- Historical stress scenarios applied to today's holdings
CREATE TABLE stress_results (
    scenario          TEXT PRIMARY KEY,     -- e.g. 'COVID crash 2020'
    window_start      DATE NOT NULL,
    window_end        DATE NOT NULL,
    portfolio_pnl     DOUBLE PRECISION NOT NULL,
    portfolio_return  DOUBLE PRECISION NOT NULL,
    benchmark_return  DOUBLE PRECISION NOT NULL
);

-- Data quality check results, written by dq.py
CREATE TABLE dq_results (
    run_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    check_name  TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('PASS', 'WARN', 'FAIL')),
    detail      TEXT,
    PRIMARY KEY (run_ts, check_name)
);