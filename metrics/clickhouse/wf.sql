-- The tables of wf runs in ClickHouse. ClickHouse runs this file once, when its volume is empty.
-- Each table is a ReplacingMergeTree on its natural key, so a second export of a run replaces its rows.
-- Read them with FINAL, or with argMax over loaded_at.

CREATE DATABASE IF NOT EXISTS wf;

CREATE TABLE IF NOT EXISTS wf.runs (
    run_id String, repo_key LowCardinality(String), workflow LowCardinality(String),
    status LowCardinality(String), dry_run Bool, started DateTime64(3, 'UTC'), ended Nullable(DateTime64(3, 'UTC')),
    duration_s Nullable(Float64), cost_usd Float64, tokens UInt64, source LowCardinality(String),
    loaded_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(loaded_at) ORDER BY run_id;

CREATE TABLE IF NOT EXISTS wf.attempts (
    run_id String, workflow LowCardinality(String), node LowCardinality(String), pass UInt16, item Int16, attempt UInt16,
    kind LowCardinality(String), harness LowCardinality(String), model LowCardinality(String), effort LowCardinality(String),
    access LowCardinality(String), session_id String, session_start LowCardinality(String),
    started DateTime64(3, 'UTC'), ended Nullable(DateTime64(3, 'UTC')), duration_s Nullable(Float64),
    outcome LowCardinality(String), error String, error_class LowCardinality(String),
    tokens UInt64, input_tokens UInt64, output_tokens UInt64, cache_read_tokens UInt64, cache_write_tokens UInt64,
    reasoning_tokens UInt64, cost_usd Float64, exit_code Nullable(Int32),
    loaded_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(loaded_at) ORDER BY (run_id, node, pass, item, attempt);

CREATE TABLE IF NOT EXISTS wf.turns (
    run_id String, node LowCardinality(String), pass UInt16, item Int16, attempt UInt16, turn UInt16,
    started DateTime64(3, 'UTC'), ended Nullable(DateTime64(3, 'UTC')), duration_s Nullable(Float64),
    status LowCardinality(String), message String,
    tokens UInt64, input_tokens UInt64, output_tokens UInt64, cache_read_tokens UInt64, cache_write_tokens UInt64,
    reasoning_tokens UInt64, cost_usd Float64,
    loaded_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(loaded_at) ORDER BY (run_id, node, pass, item, attempt, turn);

CREATE TABLE IF NOT EXISTS wf.tool_calls (
    run_id String, workflow LowCardinality(String), node LowCardinality(String), pass UInt16, item Int16, attempt UInt16,
    turn UInt16, call_id String, tool LowCardinality(String), harness LowCardinality(String), model LowCardinality(String),
    started DateTime64(3, 'UTC'), duration_ms Nullable(Float64), is_error Nullable(Bool),
    loaded_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(loaded_at) PARTITION BY toYYYYMM(started) ORDER BY (run_id, node, pass, item, attempt, call_id);

CREATE TABLE IF NOT EXISTS wf.routes (
    run_id String, node LowCardinality(String), pass UInt16, time DateTime64(3, 'UTC'), taken Int16, to Array(String),
    loaded_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(loaded_at) ORDER BY (run_id, node, pass);

CREATE TABLE IF NOT EXISTS wf.metric_points (
    run_id String, workflow LowCardinality(String), metric LowCardinality(String), node LowCardinality(String),
    pass UInt16, item Int16, time DateTime64(3, 'UTC'), value Float64, labels Map(LowCardinality(String), String),
    loaded_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(loaded_at) PARTITION BY toYYYYMM(time)
  ORDER BY (workflow, metric, run_id, node, pass, item, labels);
