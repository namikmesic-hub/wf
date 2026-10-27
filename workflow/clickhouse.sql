-- The tables of wf runs in ClickHouse, in the database of metrics.url in config.toml.
-- `wf metrics export` (export.py) makes the database and each table that is missing, before it writes rows.
-- Each table is a ReplacingMergeTree on its natural key, so a second export of a run replaces its rows.
-- Read them with FINAL, or with argMax over loaded_at.
-- An item of -1 is a job that isn't an item of a for_each node.

CREATE TABLE IF NOT EXISTS runs (
    run_id String, repo_key LowCardinality(String), workflow LowCardinality(String),
    status LowCardinality(String), dry_run Bool, started DateTime64(3, 'UTC'), ended Nullable(DateTime64(3, 'UTC')),
    duration_s Nullable(Float64), cost_usd Float64, tokens UInt64, source LowCardinality(String),
    loaded_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(loaded_at) ORDER BY run_id;

CREATE TABLE IF NOT EXISTS attempts (
    run_id String, workflow LowCardinality(String), node LowCardinality(String), pass UInt16, item Int16, attempt UInt16,
    kind LowCardinality(String), harness LowCardinality(String), model LowCardinality(String), effort LowCardinality(String),
    access LowCardinality(String), session_id String, session_start LowCardinality(String),
    started DateTime64(3, 'UTC'), ended Nullable(DateTime64(3, 'UTC')), duration_s Nullable(Float64),
    duration_basis Nullable(String), awake_s Nullable(Float64), measure_version Nullable(UInt8),
    startup_s Nullable(Float64), asleep_s Nullable(Float64), tool_s Nullable(Float64),
    active_s Nullable(Float64), idle_s Nullable(Float64), slot_wait_s Nullable(Float64), longest_gap_s Nullable(Float64),
    longest_gap_at Nullable(DateTime64(3, 'UTC')), last_progress_at Nullable(DateTime64(3, 'UTC')),
    stall_detected_at Nullable(DateTime64(3, 'UTC')), stall_action Nullable(String), sdk_retries Nullable(UInt32),
    outcome LowCardinality(String), error String, error_class LowCardinality(String),
    tokens UInt64, input_tokens UInt64, output_tokens UInt64, cache_read_tokens UInt64, cache_write_tokens UInt64,
    reasoning_tokens UInt64, cost_usd Float64, exit_code Nullable(Int32),
    loaded_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(loaded_at) ORDER BY (run_id, node, pass, item, attempt);

CREATE TABLE IF NOT EXISTS turns (
    run_id String, node LowCardinality(String), pass UInt16, item Int16, attempt UInt16, turn UInt16,
    started DateTime64(3, 'UTC'), ended Nullable(DateTime64(3, 'UTC')), duration_s Nullable(Float64),
    status LowCardinality(String), message String,
    tokens UInt64, input_tokens UInt64, output_tokens UInt64, cache_read_tokens UInt64, cache_write_tokens UInt64,
    reasoning_tokens UInt64, cost_usd Float64,
    loaded_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(loaded_at) ORDER BY (run_id, node, pass, item, attempt, turn);

CREATE TABLE IF NOT EXISTS tool_calls (
    run_id String, workflow LowCardinality(String), node LowCardinality(String), pass UInt16, item Int16, attempt UInt16,
    turn UInt16, call_id String, tool LowCardinality(String), harness LowCardinality(String), model LowCardinality(String),
    started DateTime64(3, 'UTC'), duration_ms Nullable(Float64), is_error Nullable(Bool),
    loaded_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(loaded_at) PARTITION BY toYYYYMM(started) ORDER BY (run_id, node, pass, item, attempt, call_id);

CREATE TABLE IF NOT EXISTS routes (
    run_id String, node LowCardinality(String), pass UInt16, time DateTime64(3, 'UTC'), taken Int16, to Array(String),
    loaded_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(loaded_at) ORDER BY (run_id, node, pass);

CREATE TABLE IF NOT EXISTS metric_points (
    run_id String, workflow LowCardinality(String), metric LowCardinality(String), node LowCardinality(String),
    pass UInt16, item Int16, harness LowCardinality(String), model LowCardinality(String), effort LowCardinality(String),
    time DateTime64(3, 'UTC'), value Float64, labels Map(LowCardinality(String), String),
    loaded_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(loaded_at) PARTITION BY toYYYYMM(time)
  ORDER BY (workflow, metric, run_id, node, pass, item, labels);
