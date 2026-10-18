DROP VIEW IF EXISTS v_calendar CASCADE;

-- 1. Trading calendar: every day the XIU ETF traded on the TSX
CREATE VIEW v_calendar AS
SELECT price_date
FROM prices
WHERE ticker = 'XIU';

-- 2. A price for every stock on every trading day.
--    If a stock has no price on a day (halt, data gap), the last known price is carried forward.
CREATE MATERIALIZED VIEW v_prices_filled AS
WITH grid AS (
    SELECT c.price_date, s.ticker
    FROM v_calendar c
    CROSS JOIN securities s
    WHERE s.security_type = 'Equity'
), joined AS (
    SELECT g.price_date, g.ticker, p.adj_close,
           -- running count of real prices: stays flat across gaps, so gap days share a group
           COUNT(p.adj_close) OVER (PARTITION BY g.ticker ORDER BY g.price_date) AS grp
    FROM grid g
    LEFT JOIN prices p ON p.ticker = g.ticker AND p.price_date = g.price_date
)
SELECT price_date, ticker,
       FIRST_VALUE(adj_close) OVER (PARTITION BY ticker, grp ORDER BY price_date) AS adj_close,
       adj_close IS NULL AS is_filled
FROM joined; 
CREATE UNIQUE INDEX idx_prices_filled ON v_prices_filled (ticker, price_date);

-- 3. Daily return for every stock
CREATE VIEW v_stock_returns AS
SELECT price_date, ticker, adj_close,
       adj_close / LAG(adj_close) OVER (PARTITION BY ticker ORDER BY price_date) - 1 AS ret
FROM v_prices_filled;

-- 4. Portfolio value and return each day since the start date (fixed share counts)
CREATE VIEW v_portfolio_daily AS
WITH mv AS (
    SELECT r.price_date, SUM(h.shares * r.adj_close) AS market_value
    FROM v_stock_returns r
    JOIN holdings h ON h.ticker = r.ticker
    WHERE r.price_date >= (SELECT MIN(start_date) FROM holdings)
    GROUP BY r.price_date
)
SELECT price_date, market_value,
       market_value / LAG(market_value) OVER (ORDER BY price_date) - 1 AS daily_return
FROM mv;

-- 5. Benchmark: buy-and-hold of TSX 60 "implied shares".
--    The XIU file gives weights at the END of the period. Dividing each weight by that
--    day's price backs out an implied share count, so weights drift with prices like the
--    real index instead of using end-of-period weights with hindsight.
CREATE VIEW v_benchmark_daily AS
WITH latest AS (
    SELECT MAX(as_of_date) AS as_of FROM benchmark_weights
), anchor_day AS (
    -- last trading day on or before the weights' as-of date, found once
    SELECT MAX(c.price_date) AS d
    FROM v_calendar c CROSS JOIN latest l
    WHERE c.price_date <= l.as_of
), anchor AS (
    SELECT bw.ticker, bw.weight / f.adj_close AS implied_shares
    FROM benchmark_weights bw
    JOIN latest l ON bw.as_of_date = l.as_of
    JOIN anchor_day a ON TRUE
    JOIN v_prices_filled f ON f.ticker = bw.ticker AND f.price_date = a.d
), mv AS (
    SELECT f.price_date, SUM(a.implied_shares * f.adj_close) AS market_value
    FROM v_prices_filled f
    JOIN anchor a ON a.ticker = f.ticker
    GROUP BY f.price_date
    HAVING COUNT(f.adj_close) = COUNT(*)   -- only days when every constituent has a price
)
SELECT price_date, market_value,
       market_value / LAG(market_value) OVER (ORDER BY price_date) - 1 AS daily_return
FROM mv;

-- 6. One row per day: portfolio vs. benchmark vs. actual XIU, cumulative returns, drawdown
CREATE VIEW v_performance_daily AS
WITH xiu AS (
    SELECT price_date,
           adj_close / LAG(adj_close) OVER (ORDER BY price_date) - 1 AS xiu_return
    FROM prices
    WHERE ticker = 'XIU'
), base AS (
    SELECT p.price_date, p.market_value,
           p.daily_return AS port_return,
           -- on the start day nothing has been earned yet, so blank all returns
           CASE WHEN p.daily_return IS NULL THEN NULL ELSE b.daily_return END AS bench_return,
           CASE WHEN p.daily_return IS NULL THEN NULL ELSE x.xiu_return END AS xiu_return
    FROM v_portfolio_daily p
    LEFT JOIN v_benchmark_daily b USING (price_date)
    LEFT JOIN xiu x USING (price_date)
)
SELECT price_date, market_value, port_return, bench_return, xiu_return,
       port_return - bench_return AS active_return,
       EXP(SUM(LN(1 + COALESCE(port_return, 0))) OVER w) - 1 AS port_cum_return,
       EXP(SUM(LN(1 + COALESCE(bench_return, 0))) OVER w) - 1 AS bench_cum_return,
       market_value / MAX(market_value) OVER w - 1 AS drawdown
FROM base
WINDOW w AS (ORDER BY price_date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW);

-- 7. One-row summary of the whole period
CREATE VIEW v_performance_summary AS
WITH params AS (
    -- Approximate average Bank of Canada 3-month T-bill yield, Oct 2025 - Oct 2026 (about 2.2 to 2.4 percent)
    SELECT 0.023::float8 AS risk_free_rate
), d AS (
    SELECT * FROM v_performance_daily WHERE port_return IS NOT NULL
), agg AS (
    SELECT MIN(price_date)                                       AS first_return_date,
           MAX(price_date)                                       AS end_date,
           COUNT(*)                                              AS trading_days,
           EXP(SUM(LN(1 + port_return))) - 1                     AS port_return,
           EXP(SUM(LN(1 + bench_return))) - 1                    AS bench_return,
           EXP(SUM(LN(1 + xiu_return))) - 1                      AS xiu_return,
           STDDEV_SAMP(port_return) * SQRT(252)                  AS port_vol,
           STDDEV_SAMP(bench_return) * SQRT(252)                 AS bench_vol,
           STDDEV_SAMP(port_return - bench_return) * SQRT(252)   AS tracking_error,
           REGR_SLOPE(port_return, bench_return)                 AS beta,
           MIN(drawdown)                                         AS max_drawdown
    FROM d
), ann AS (
    SELECT a.*,
           POWER(1 + a.port_return,  252.0::float8 / a.trading_days) - 1 AS port_return_ann,
           POWER(1 + a.bench_return, 252.0::float8 / a.trading_days) - 1 AS bench_return_ann
    FROM agg a
)
SELECT ann.*,
       ann.port_return - ann.bench_return                                   AS active_return,
       (ann.port_return_ann - p.risk_free_rate) / ann.port_vol             AS sharpe,
       (ann.port_return_ann - ann.bench_return_ann) / ann.tracking_error   AS information_ratio
FROM ann
CROSS JOIN params p;