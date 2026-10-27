ALTER TABLE attempts ADD COLUMN IF NOT EXISTS duration_basis Nullable(String);
ALTER TABLE attempts ADD COLUMN IF NOT EXISTS awake_s Nullable(Float64);
ALTER TABLE attempts ADD COLUMN IF NOT EXISTS asleep_s Nullable(Float64);

CREATE TABLE IF NOT EXISTS export_runs (
    started_at DateTime64(6, 'UTC'), finished_at DateTime64(6, 'UTC'), runs_read UInt64, rows_written UInt64,
    error_class LowCardinality(String), loaded_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(loaded_at) ORDER BY started_at;

CREATE OR REPLACE VIEW attempt_times AS
SELECT a.run_id AS run_id, a.workflow AS workflow, a.node AS node, a.pass AS pass, a.item AS item,
       a.attempt AS attempt, a.kind AS kind, a.harness AS harness, a.model AS model,
       a.started AS started, a.ended AS ended, a.duration_s AS duration_s,
       dateDiff('millisecond', a.started, a.ended) / 1000 AS wall_s,
       ifNull(a.awake_s, a.duration_s) AS awake_s,
       ifNull(a.asleep_s, greatest(wall_s - a.duration_s, 0)) AS asleep_s
FROM attempts AS a FINAL
INNER JOIN (SELECT run_id FROM runs FINAL WHERE source = 'trace') AS r ON r.run_id = a.run_id
WHERE a.ended IS NOT NULL AND a.duration_s IS NOT NULL AND a.outcome != 'cancelled'
  AND a.kind IN ('agent', 'command') AND (a.duration_basis IS NULL OR a.duration_basis = 'monotonic');
