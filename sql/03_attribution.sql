-- 03_attribution.sql: daily weights, sector exposures, Brinson attribution, contributions.
-- Run AFTER 02_performance.sql (it depends on those views).

DROP VIEW IF EXISTS v_contributions, v_brinson_summary, v_brinson_daily,
    v_holdings_current, v_exposures_current, v_security_daily, v_benchmark_shares CASCADE;

-- 1. Implied benchmark share counts (weight / price on the weights' as-of date)
CREATE VIEW v_benchmark_shares AS
WITH latest AS (
    SELECT MAX(as_of_date) AS as_of FROM benchmark_weights
), anchor_day AS (
    SELECT MAX(c.price_date) AS d
    FROM v_calendar c CROSS JOIN latest l
    WHERE c.price_date <= l.as_of
)
SELECT bw.ticker, bw.weight / f.adj_close AS implied_shares
FROM benchmark_weights bw
JOIN latest l ON bw.as_of_date = l.as_of
JOIN anchor_day a ON TRUE
JOIN v_prices_filled f ON f.ticker = bw.ticker AND f.price_date = a.d;

-- 2. One row per stock per day: return, portfolio and benchmark weights (today and yesterday)
CREATE VIEW v_security_daily AS
WITH vals AS (
    SELECT r.price_date, r.ticker, s.sector, r.ret,
           COALESCE(h.shares, 0)         * r.adj_close AS port_value,
           COALESCE(b.implied_shares, 0) * r.adj_close AS bench_value
    FROM v_stock_returns r
    JOIN securities s ON s.ticker = r.ticker
    LEFT JOIN holdings h ON h.ticker = r.ticker
    LEFT JOIN v_benchmark_shares b ON b.ticker = r.ticker
    WHERE r.price_date >= (SELECT MIN(start_date) FROM holdings)
      AND (h.ticker IS NOT NULL OR b.ticker IS NOT NULL)
), w AS (
    SELECT *,
           port_value  / SUM(port_value)  OVER (PARTITION BY price_date) AS port_weight,
           bench_value / SUM(bench_value) OVER (PARTITION BY price_date) AS bench_weight
    FROM vals
)
SELECT price_date, ticker, sector, ret, port_value, bench_value, port_weight, bench_weight,
       -- yesterday's weights: the ones that earn today's return
       LAG(port_weight)  OVER (PARTITION BY ticker ORDER BY price_date) AS port_weight_prev,
       LAG(bench_weight) OVER (PARTITION BY ticker ORDER BY price_date) AS bench_weight_prev
FROM w;

-- 3. Current sector exposures: portfolio vs. benchmark (latest date)
CREATE VIEW v_exposures_current AS
SELECT sector,
       SUM(port_weight)                     AS port_weight,
       SUM(bench_weight)                    AS bench_weight,
       SUM(port_weight) - SUM(bench_weight) AS active_weight
FROM v_security_daily
WHERE price_date = (SELECT MAX(price_date) FROM v_security_daily)
GROUP BY sector;

-- 4. Current stock-level weights (for the top holdings table)
CREATE VIEW v_holdings_current AS
SELECT s.ticker, sec.name, s.sector, s.port_value, s.port_weight, s.bench_weight,
       s.port_weight - s.bench_weight AS active_weight
FROM v_security_daily s
JOIN securities sec ON sec.ticker = s.ticker
WHERE s.price_date = (SELECT MAX(price_date) FROM v_security_daily);

-- 5. Brinson-Hood-Beebower attribution by sector and day
CREATE VIEW v_brinson_daily AS
WITH sec AS (
    SELECT price_date, sector,
           SUM(port_weight_prev)        AS wp,   -- portfolio sector weight
           SUM(bench_weight_prev)       AS wb,   -- benchmark sector weight
           SUM(port_weight_prev  * ret) AS cp,   -- sector contribution, portfolio
           SUM(bench_weight_prev * ret) AS cb    -- sector contribution, benchmark
    FROM v_security_daily
    WHERE port_weight_prev IS NOT NULL AND ret IS NOT NULL
    GROUP BY price_date, sector
), r AS (
    SELECT *,
           SUM(cp) OVER (PARTITION BY price_date) AS rp_total,
           SUM(cb) OVER (PARTITION BY price_date) AS rb_total,
           CASE WHEN wb > 0 THEN cb / wb END     AS rb_s_raw,
           CASE WHEN wp > 0 THEN cp / wp END     AS rp_s_raw
    FROM sec
), r2 AS (
    SELECT *,
           COALESCE(rb_s_raw, rb_total)                   AS rb_s,  -- sector not in benchmark
           COALESCE(rp_s_raw, COALESCE(rb_s_raw, rb_total)) AS rp_s -- sector not held: no selection effect
    FROM r
)
SELECT price_date, sector, wp, wb, rp_s, rb_s, rp_total, rb_total,
       (wp - wb) * (rb_s - rb_total) AS allocation,
       wb * (rp_s - rb_s)            AS selection,
       (wp - wb) * (rp_s - rb_s)     AS interaction
FROM r2;

-- 6. Attribution summed over the whole period, by sector
CREATE VIEW v_brinson_summary AS
SELECT sector,
       AVG(wp)                                   AS avg_port_weight,
       AVG(wb)                                   AS avg_bench_weight,
       SUM(allocation)                           AS allocation,
       SUM(selection)                            AS selection,
       SUM(interaction)                          AS interaction,
       SUM(allocation + selection + interaction) AS total_effect
FROM v_brinson_daily
GROUP BY sector;

-- 7. Stock-level contributions over the whole period
CREATE VIEW v_contributions AS
WITH bench AS (
    SELECT price_date, SUM(bench_weight_prev * ret) AS rb
    FROM v_security_daily
    WHERE ret IS NOT NULL AND bench_weight_prev IS NOT NULL
    GROUP BY price_date
)
SELECT s.ticker, sec.name, s.sector,
       BOOL_OR(s.port_value > 0)                                   AS held,
       SUM(s.port_weight_prev * s.ret)                             AS contribution,
       -- effect on active return: over/underweight x how the stock did vs. the benchmark
       SUM((s.port_weight_prev - s.bench_weight_prev) * (s.ret - b.rb)) AS active_contribution
FROM v_security_daily s
JOIN bench b ON b.price_date = s.price_date
JOIN securities sec ON sec.ticker = s.ticker
WHERE s.ret IS NOT NULL AND s.port_weight_prev IS NOT NULL
GROUP BY s.ticker, sec.name, s.sector;