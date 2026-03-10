# OSCAR KORE — SQL to PromQL Query Guide

> **One-stop reference** for every query you can run against a Prometheus-compatible datasource
> (VictoriaMetrics, Thanos, Mimir, Cortex) through OSCAR's KORE Editor.

---

## Setup

```sql
-- Register the datasource once (run in KORE Editor)
CREATE DATABASE victoriametrics
WITH ENGINE = 'prometheus',
PARAMETERS = {
  "host": "http://vmdb:8428",
  "timeout": 30
};
```

```sql
-- Confirm it registered
SHOW DATABASES;

-- See all available tables
SHOW TABLES FROM victoriametrics;
```

> **Tables available:** `instant`, `range_query`, `metrics`, `labels` + 8 named OSCAR node tables (see below)

---

## Table Overview

| Table | Type | Returns | Use For |
|-------|------|---------|---------|
| `instant` | Generic | One row per time series | Current value for any metric |
| `range_query` | Generic | One row per (series × timestamp) | Trends/history for any metric |
| `metrics` | Discovery | One row per metric name | What metrics exist in VictoriaMetrics |
| `labels` | Discovery | One row per label combination | What labels/values a metric has |
| `oscar_node_cpu_utilization` | Named | One row per (instance × timestamp) | CPU usage % — no `WHERE metric` needed |
| `oscar_node_memory_utilization` | Named | One row per (instance × timestamp) | Memory usage % (excl. buffers/cache) |
| `oscar_node_swap_utilization` | Named | One row per (instance × timestamp) | Swap usage % |
| `oscar_node_disk_utilization` | Named | One row per (instance × mountpoint × timestamp) | Disk usage % per mountpoint |
| `oscar_node_iowait_pct` | Named | One row per (instance × timestamp) | I/O wait % |
| `oscar_node_load_per_cpu` | Named | One row per (instance × timestamp) | Load average per vCPU |
| `oscar_node_network_rx_bytes_rate` | Named | One row per (instance × timestamp) | Network RX bytes/s |
| `oscar_node_network_tx_bytes_rate` | Named | One row per (instance × timestamp) | Network TX bytes/s |

---

## WHERE Clause Reference

### Routing Parameters (never become PromQL label selectors)

| SQL Column | Type | Description | Example |
|------------|------|-------------|---------|
| `metric` | required | Metric name | `metric = 'up'` |
| `fn` | optional | PromQL function (see below) | `fn = 'rate'` |
| `fn_labels` | optional | `by(...)` label list for aggregation | `fn_labels = 'job,instance'` |
| `fn_window` | optional | Range vector duration (default `5m`) | `fn_window = '10m'` |
| `value` | optional | Threshold filter appended to expression | `value = '< 0.8'` |
| `time` | optional | Point-in-time for instant queries (Unix or RFC3339) | `time = '2024-01-01T00:00:00Z'` |
| `time_start` | optional (range_query) | Range start (default `now-1h`) | `time_start = 'now-6h'` |
| `time_end` | optional (range_query) | Range end (default `now`) | `time_end = 'now'` |
| `step` | optional (range_query) | Resolution step (default `1m`) | `step = '30s'` |

> **Reserved word note:** `start`, `end`, and `range` are SQL reserved words and cannot be used as-is.
>
> | Reserved word | Problem | Solution applied |
> |---------------|---------|-----------------|
> | `start` | WHERE column name | Use `time_start` instead (or `` `start` `` backtick-quoted) |
> | `end` | WHERE column name | Use `time_end` instead (or `` `end` `` backtick-quoted) |
> | `range` | Table name | Table is named `range_query` instead |
> | `window` | WHERE column name | Use `fn_window` instead |

### Label Selectors (any other column → PromQL label)

| SQL Operator | PromQL Operator | Meaning |
|-------------|----------------|---------|
| `= 'value'` | `="value"` | Exact match |
| `!= 'value'` | `!="value"` | Exclude exact match |
| `LIKE 'pattern'` | `=~"pattern"` | Regex match |
| `NOT LIKE 'pattern'` | `!~"pattern"` | Regex exclude |

### Supported `fn` Values

**Range functions** (require a `[window]` vector):

| fn value | PromQL | Description |
|----------|--------|-------------|
| `rate` | `rate(metric[w])` | Per-second average increase rate |
| `irate` | `irate(metric[w])` | Instantaneous rate (last 2 samples) |
| `increase` | `increase(metric[w])` | Total increase over window |
| `avg_over_time` | `avg_over_time(metric[w])` | Average value over window |
| `max_over_time` | `max_over_time(metric[w])` | Maximum value over window |
| `min_over_time` | `min_over_time(metric[w])` | Minimum value over window |
| `sum_over_time` | `sum_over_time(metric[w])` | Sum of values over window |
| `count_over_time` | `count_over_time(metric[w])` | Count of samples over window |

**Aggregation functions** (group across series):

| fn value | PromQL | Description |
|----------|--------|-------------|
| `sum` | `sum by(...) (metric)` | Sum across series |
| `avg` | `avg by(...) (metric)` | Average across series |
| `max` | `max by(...) (metric)` | Maximum across series |
| `min` | `min by(...) (metric)` | Minimum across series |
| `count` | `count by(...) (metric)` | Count of series |
| `stddev` | `stddev by(...) (metric)` | Standard deviation |
| `topk` | `topk(N, metric)` | Top N series |
| `bottomk` | `bottomk(N, metric)` | Bottom N series |

**Compound functions** (`fn = 'outer:inner'`):

| fn value | PromQL | Description |
|----------|--------|-------------|
| `sum:rate` | `sum by(...) (rate(metric[w]))` | Sum of per-second rates |
| `avg:rate` | `avg by(...) (rate(metric[w]))` | Average of per-second rates |
| `max:rate` | `max by(...) (rate(metric[w]))` | Max of per-second rates |
| `sum:increase` | `sum by(...) (increase(metric[w]))` | Sum of total increases |

---

## Table 1: `instant` — Current Value Snapshot

> **Fires:** `GET /api/v1/query`
> **Returns:** One row per matching time series, at the current moment (or a specified `time`).
> **Columns:** `metric`, `timestamp`, `value` + all Prometheus labels as dynamic columns (e.g. `job`, `instance`)

---

### 1.1 — Simple metric lookup

```sql
-- SQL
SELECT * FROM victoriametrics.instant
WHERE metric = 'up';

-- PromQL fired: up
```

---

### 1.2 — Filter by label (exact match)

```sql
-- SQL
SELECT * FROM victoriametrics.instant
WHERE metric = 'up'
  AND job = 'alertmanager';

-- PromQL fired: up{job="alertmanager"}
```

---

### 1.3 — Multiple label filters

```sql
-- SQL
SELECT metric, job, instance, value FROM victoriametrics.instant
WHERE metric = 'up'
  AND job = 'alertmanager'
  AND datacenter = 'dc1';

-- PromQL fired: up{job="alertmanager", datacenter="dc1"}
```

---

### 1.4 — Regex label match (LIKE)

```sql
-- SQL
SELECT * FROM victoriametrics.instant
WHERE metric = 'up'
  AND job LIKE 'alert.*';

-- PromQL fired: up{job=~"alert.*"}
```

---

### 1.5 — Regex label exclusion (NOT LIKE)

```sql
-- SQL
SELECT * FROM victoriametrics.instant
WHERE metric = 'up'
  AND job NOT LIKE 'test.*';

-- PromQL fired: up{job!~"test.*"}
```

---

### 1.6 — Label inequality (!=)

```sql
-- SQL
SELECT * FROM victoriametrics.instant
WHERE metric = 'up'
  AND instance != 'localhost:9090';

-- PromQL fired: up{instance!="localhost:9090"}
```

---

### 1.7 — Point-in-time query

```sql
-- SQL
SELECT * FROM victoriametrics.instant
WHERE metric = 'up'
  AND time = '2024-06-01T12:00:00Z';

-- PromQL fired: up  (with ?time=2024-06-01T12:00:00Z)
```

---

### 1.8 — Rate function (per-second rate of a counter)

```sql
-- SQL
SELECT * FROM victoriametrics.instant
WHERE metric = 'http_requests_total'
  AND job = 'api'
  AND fn = 'rate'
  AND fn_window = '5m';

-- PromQL fired: rate(http_requests_total{job="api"}[5m])
```

---

### 1.9 — Aggregation with grouping

```sql
-- SQL
SELECT * FROM victoriametrics.instant
WHERE metric = 'http_requests_total'
  AND fn = 'sum'
  AND fn_labels = 'job';

-- PromQL fired: sum by(job) (http_requests_total)
```

---

### 1.10 — Sum of rates (compound function)

```sql
-- SQL
SELECT * FROM victoriametrics.instant
WHERE metric = 'http_requests_total'
  AND fn = 'sum:rate'
  AND fn_labels = 'job,instance'
  AND fn_window = '5m';

-- PromQL fired: sum by(job, instance) (rate(http_requests_total[5m]))
```

---

### 1.11 — Threshold filter (value)

```sql
-- SQL — find all instances where 'up' metric is 0 (down)
SELECT * FROM victoriametrics.instant
WHERE metric = 'up'
  AND value = '== 0';

-- PromQL fired: up == 0
```

```sql
-- SQL — CPU usage above 80%
SELECT * FROM victoriametrics.instant
WHERE metric = 'node_cpu_usage_percent'
  AND value = '> 80';

-- PromQL fired: node_cpu_usage_percent > 80
```

---

### 1.12 — Limit results

```sql
-- SQL
SELECT metric, job, value FROM victoriametrics.instant
WHERE metric = 'up'
LIMIT 10;

-- PromQL fired: up  (DataFrame truncated to 10 rows)
```

---

## Table 2: `range_query` — Time Series History

> **Fires:** `GET /api/v1/query_range`
> **Returns:** One row per (time series × step interval). Large result sets are normal.
> **Defaults:** `time_start=now-1h`, `time_end=now`, `step=1m` — all time params are optional.
> **Columns:** `metric`, `timestamp`, `value` + all Prometheus labels as dynamic columns

---

### Understanding `step`

`step` is the **resolution** of the time series — how far apart each data point is.
For a range query, VictoriaMetrics returns one value per `step` interval across the `time_start → time_end` window.

| `step` | Window = 1h | Window = 24h |
|--------|-------------|--------------|
| `30s`  | 120 rows    | 2880 rows    |
| `1m`   | 60 rows     | 1440 rows    |
| `5m`   | 12 rows     | 288 rows     |
| `15m`  | 4 rows      | 96 rows      |
| `1h`   | 1 row       | 24 rows      |

**Format:** any PromQL duration string — `30s`, `1m`, `5m`, `1h`, `1d`

**Default if omitted:** `1m`

**Rule of thumb:** smaller step = more data points = more memory/bandwidth.
For dashboards showing 24h, use `5m` or `15m`. For a 1-hour window with second-level detail, use `30s`.

---

### 2.1 — Basic range query

```sql
-- SQL
SELECT * FROM victoriametrics.range_query
WHERE metric = 'up'
  AND time_start = 'now-1h'
  AND time_end = 'now'
  AND step = '1m';

-- PromQL fired: up  (with start=now-1h, end=now, step=1m)
-- Returns 60 rows — one per minute over the last hour
```

---

### 2.2 — Select specific columns

```sql
-- SQL
SELECT metric, job, instance, timestamp, value FROM victoriametrics.range_query
WHERE metric = 'up'
  AND time_start = 'now-1h'
  AND time_end = 'now'
  AND step = '1m';

-- PromQL fired: up
-- Returns only: metric, job, instance, timestamp, value columns
```

---

### 2.3 — Filter by label over time

```sql
-- SQL
SELECT * FROM victoriametrics.range_query
WHERE metric = 'up'
  AND job = 'alertmanager'
  AND time_start = 'now-6h'
  AND time_end = 'now'
  AND step = '5m';

-- PromQL fired: up{job="alertmanager"}
```

---

### 2.4 — Backtick syntax for reserved words (alternative)

```sql
-- SQL — identical to using time_start/time_end
SELECT * FROM victoriametrics.range_query
WHERE metric = 'up'
  AND `start` = 'now-1h'
  AND `end` = 'now'
  AND step = '1m';

-- PromQL fired: up
```

---

### 2.5 — Rate over time (rolling per-second rate)

```sql
-- SQL
SELECT * FROM victoriametrics.range_query
WHERE metric = 'http_requests_total'
  AND job = 'api'
  AND fn = 'rate'
  AND fn_window = '5m'
  AND time_start = 'now-1h'
  AND time_end = 'now'
  AND step = '1m';

-- PromQL fired: rate(http_requests_total{job="api"}[5m])
```

---

### 2.6 — Aggregated rate over time

```sql
-- SQL — total request rate across all instances, grouped by job
SELECT * FROM victoriametrics.range_query
WHERE metric = 'http_requests_total'
  AND fn = 'sum:rate'
  AND fn_labels = 'job'
  AND fn_window = '5m'
  AND time_start = 'now-3h'
  AND time_end = 'now'
  AND step = '1m';

-- PromQL fired: sum by(job) (rate(http_requests_total[5m]))
```

---

### 2.7 — Average CPU over time, grouped by instance

```sql
-- SQL
SELECT * FROM victoriametrics.range_query
WHERE metric = 'node_cpu_seconds_total'
  AND mode = 'idle'
  AND fn = 'avg:rate'
  AND fn_labels = 'instance'
  AND fn_window = '5m'
  AND time_start = 'now-24h'
  AND time_end = 'now'
  AND step = '15m';

-- PromQL fired: avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))
```

---

### 2.8 — Absolute timestamps

```sql
-- SQL
SELECT * FROM victoriametrics.range_query
WHERE metric = 'up'
  AND time_start = '2024-06-01T00:00:00Z'
  AND time_end = '2024-06-01T06:00:00Z'
  AND step = '5m';

-- PromQL fired: up  (with absolute RFC3339 timestamps)
```

---

### 2.9 — Unix epoch timestamps

```sql
-- SQL
SELECT * FROM victoriametrics.range_query
WHERE metric = 'up'
  AND time_start = '1717200000'
  AND time_end = '1717221600'
  AND step = '1m';

-- PromQL fired: up  (with Unix timestamps)
```

---

### 2.10 — High-resolution short window

```sql
-- SQL — last 10 minutes at 15-second resolution
SELECT metric, job, instance, timestamp, value FROM victoriametrics.range_query
WHERE metric = 'node_memory_MemAvailable_bytes'
  AND time_start = 'now-10m'
  AND time_end = 'now'
  AND step = '15s';

-- PromQL fired: node_memory_MemAvailable_bytes
```

---

### 2.11 — Threshold filter over time (find degraded periods)

```sql
-- SQL — only return rows where 'up' was 0
SELECT metric, job, instance, timestamp, value FROM victoriametrics.range_query
WHERE metric = 'up'
  AND value = '== 0'
  AND time_start = 'now-24h'
  AND time_end = 'now'
  AND step = '1m';

-- PromQL fired: up == 0  (VictoriaMetrics returns only matching samples)
```

---

### 2.12 — Limit rows returned

```sql
-- SQL
SELECT * FROM victoriametrics.range_query
WHERE metric = 'up'
  AND time_start = 'now-1h'
  AND time_end = 'now'
  AND step = '1m'
LIMIT 100;

-- PromQL fired: up  (DataFrame truncated to 100 rows after fetch)
```

---

## Table 3: `metrics` — Metric Discovery

> **Fires:** `GET /api/v1/label/__name__/values`
> **Returns:** One row per metric name stored in VictoriaMetrics.
> **Column:** `metric_name`
> **No WHERE filtering** — always returns the full list.

---

### 3.1 — List all metrics

```sql
SELECT * FROM victoriametrics.metrics;

-- Returns: all metric names, e.g.:
-- up, node_cpu_seconds_total, http_requests_total, ...
```

---

### 3.2 — Limit to first N metrics

```sql
SELECT * FROM victoriametrics.metrics
LIMIT 20;
```

---

### 3.3 — Use result to explore a specific metric

```sql
-- Step 1: find metrics with 'cpu' in the name
SELECT * FROM victoriametrics.metrics
LIMIT 200;
-- (then filter visually or in follow-up queries)

-- Step 2: query the metric you found
SELECT * FROM victoriametrics.instant
WHERE metric = 'node_cpu_seconds_total'
LIMIT 10;
```

---

## Table 4: `labels` — Label Discovery

> **Fires:** `GET /api/v1/series`
> **Returns:** One row per unique label combination for a metric.
> **Columns:** `metric` + all label names present on that metric (dynamic).

---

### 4.1 — All label combinations for a metric

```sql
SELECT * FROM victoriametrics.labels
WHERE metric = 'up';

-- Returns: all {job, instance, datacenter, ...} combinations for 'up'
```

---

### 4.2 — Discover labels for a node metric

```sql
SELECT * FROM victoriametrics.labels
WHERE metric = 'node_cpu_seconds_total';

-- Returns every label set: job, instance, cpu, mode, etc.
```

---

### 4.3 — Limit label results

```sql
SELECT * FROM victoriametrics.labels
WHERE metric = 'http_requests_total'
LIMIT 50;
```

---

### 4.4 — Workflow: discover then query

```sql
-- Step 1: what labels does 'up' have?
SELECT * FROM victoriametrics.labels
WHERE metric = 'up';

-- Step 2: use discovered label values in an instant query
SELECT * FROM victoriametrics.instant
WHERE metric = 'up'
  AND job = 'node-exporter'   -- value found in step 1
  AND datacenter = 'eu-west'; -- value found in step 1
```

---

## Common Patterns

### Pattern A — Is anything down right now?

```sql
SELECT metric, job, instance, value FROM victoriametrics.instant
WHERE metric = 'up'
  AND value = '== 0';

-- PromQL: up == 0
```

### Pattern B — Request rate per service (last 5 min)

```sql
SELECT * FROM victoriametrics.instant
WHERE metric = 'http_requests_total'
  AND fn = 'sum:rate'
  AND fn_labels = 'job'
  AND fn_window = '5m';

-- PromQL: sum by(job) (rate(http_requests_total[5m]))
```

### Pattern C — Memory usage trend (last 6 hours)

```sql
-- Step 1: discover what labels this metric actually has
SELECT * FROM victoriametrics.labels
WHERE metric = 'node_memory_MemAvailable_bytes';

-- Step 2: query using only columns that exist (use SELECT * if unsure)
SELECT * FROM victoriametrics.range_query
WHERE metric = 'container_fs_inodes_total'
  AND fn = 'avg_over_time'
  AND fn_window = '5m'
  AND time_start = 'now-6h'
  AND time_end = 'now'
  AND step = '5m';

-- PromQL: avg_over_time(node_memory_MemAvailable_bytes[5m])  (with start=now-6h, end=now, step=5m)
```

> **Note:** Always use `SELECT *` or check the `labels` table first before selecting specific label columns.
> If you select `job, instance` but the metric doesn't have those labels, DuckDB will throw:
> `Binder Error: Referenced column "job" not found in FROM clause`

### Pattern D — CPU saturation alert check

```sql
SELECT * FROM victoriametrics.instant
WHERE metric = 'node_cpu_usage_percent'
  AND fn = 'avg'
  AND fn_labels = 'instance'
  AND value = '> 90';

-- PromQL: avg by(instance) (node_cpu_usage_percent) > 90
```

### Pattern E — Error rate spike in last hour

```sql
SELECT job, timestamp, value FROM victoriametrics.range_query
WHERE metric = 'http_errors_total'
  AND fn = 'sum:rate'
  AND fn_labels = 'job'
  AND fn_window = '5m'
  AND time_start = 'now-1h'
  AND time_end = 'now'
  AND step = '1m'
  AND value = '> 0';

-- PromQL: sum by(job) (rate(http_errors_total[5m])) > 0  (with start=now-1h, end=now, step=1m)
```

### Pattern F — Disk space below threshold

```sql
SELECT instance, mountpoint, value FROM victoriametrics.instant
WHERE metric = 'node_filesystem_avail_bytes'
  AND fstype LIKE 'ext.*'
  AND value = '< 10737418240';  -- less than 10 GB

-- PromQL: node_filesystem_avail_bytes{fstype=~"ext.*"} < 10737418240
```

---

## Auditing Queries

Every successful query is automatically written to the OSCAR user audit log (`UA_User_Audit`) as:

| Field | Value |
|-------|-------|
| `action` | `read` |
| `resource_type` | `kore_query` |
| `resource_name` | e.g. `victoriametrics.range_query` |
| `summary` | `PromQL: up{job="alertmanager"}` |
| `details.promql` | the exact PromQL string fired |
| `details.endpoint` | `/api/v1/query` or `/api/v1/query_range` |

View audit log from KORE Editor (requires `read:user-audit` permission):

```sql
-- Not available via MindsDB SQL — query via OSCAR Admin UI
-- Admin → Audit Logs → filter resource_type = kore_query
```

To verify the fired PromQL in real time, check the MindsDB container logs:

```bash
docker logs oscar-kore --tail 50 -f | grep prometheus_handler
# [prometheus_handler] instant query → PromQL: up{job="alertmanager"}
# [prometheus_handler] range query  → PromQL: rate(http_requests_total[5m])
```

---

---

## OSCAR Node Metrics — Named Tables

> **Source:** `oscar/oscar-metricstore/vmalert/rules/recording-oscar-node-derived.on.yml`
>
> Each `oscar:node:*` recording rule has a **dedicated named table** — no `WHERE metric = ...` required.
> The metric is baked into the table. All other WHERE conditions become PromQL label selectors.
>
> **Default behaviour (no time params):** range query, last 1 hour, 1-minute step.
> **Instant query:** add `WHERE time = 'now'` (or any RFC3339 / Unix timestamp).

### Named Table Reference

| SQL Table | PromQL Metric | Key Labels |
|-----------|--------------|------------|
| `oscar_node_cpu_utilization` | `oscar:node:cpu_utilization` | `instance` |
| `oscar_node_memory_utilization` | `oscar:node:memory_utilization` | `instance` |
| `oscar_node_swap_utilization` | `oscar:node:swap_utilization` | `instance` |
| `oscar_node_disk_utilization` | `oscar:node:disk_utilization` | `instance`, `mountpoint` |
| `oscar_node_iowait_pct` | `oscar:node:iowait_pct` | `instance` |
| `oscar_node_load_per_cpu` | `oscar:node:load_per_cpu` | `instance` |
| `oscar_node_network_rx_bytes_rate` | `oscar:node:network_rx_bytes_rate` | `instance` |
| `oscar_node_network_tx_bytes_rate` | `oscar:node:network_tx_bytes_rate` | `instance` |

**Common columns on all named tables:** `metric`, `value`, `timestamp`, `instance`, `job`, `datacenter`, `environment`, `oscar_metric_type`

---

### Named Table Examples

> **Note — `value` in WHERE is a PromQL threshold filter, not a column filter.**
> `WHERE value = '> 80'` appends `> 80` to the PromQL expression, so only time series
> where the current value exceeds 80 are returned.  If no series match (e.g. all nodes
> have swap = 0), the result is an empty table — this is correct behaviour, not an error.
> All declared columns (`instance`, `value`, `timestamp`, etc.) are always present in the
> result even when empty.

#### Current CPU % across all nodes

```sql
SELECT * FROM victoriametrics.oscar_node_cpu_utilization
WHERE time = 'now';

-- PromQL: oscar:node:cpu_utilization  (instant, current)
```

#### CPU trend — last 6 hours, 5-minute resolution

```sql
SELECT instance, timestamp, value FROM victoriametrics.oscar_node_cpu_utilization
WHERE time_start = 'now-6h'
  AND time_end = 'now'
  AND step = '5m';

-- PromQL: oscar:node:cpu_utilization  (start=now-6h, end=now, step=5m)
```

#### Filter to a specific server

```sql
SELECT * FROM victoriametrics.oscar_node_cpu_utilization
WHERE instance = 'server01:9100'
  AND time_start = 'now-1h'
  AND step = '1m';

-- PromQL: oscar:node:cpu_utilization{instance="server01:9100"}
```

#### Nodes with CPU above 80% right now

```sql
SELECT instance, value FROM victoriametrics.oscar_node_cpu_utilization
WHERE time = 'now'
  AND value = '> 80';

-- PromQL: oscar:node:cpu_utilization > 80
```

#### Memory usage — current snapshot

```sql
SELECT * FROM victoriametrics.oscar_node_memory_utilization
WHERE time = 'now';

-- PromQL: oscar:node:memory_utilization
```

#### Disk usage per mountpoint — all above 70%

```sql
SELECT instance, mountpoint, value FROM victoriametrics.oscar_node_disk_utilization
WHERE time = 'now'
  AND value = '> 70';

-- PromQL: oscar:node:disk_utilization > 70
```

#### Network RX trend per instance — last 24h

```sql
SELECT instance, timestamp, value FROM victoriametrics.oscar_node_network_rx_bytes_rate
WHERE time_start = 'now-24h'
  AND time_end = 'now'
  AND step = '15m';

-- PromQL: oscar:node:network_rx_bytes_rate
```

#### Load per CPU — nodes above 1.0 (saturated)

```sql
SELECT instance, value FROM victoriametrics.oscar_node_load_per_cpu
WHERE time = 'now'
  AND value = '> 1';

-- PromQL: oscar:node:load_per_cpu > 1
```

#### Swap usage — any node using swap

```sql
SELECT instance, value FROM victoriametrics.oscar_node_swap_utilization
WHERE time = 'now'
  AND value = '> 0';

-- PromQL: oscar:node:swap_utilization > 0
```

#### I/O wait — nodes with high iowait (> 20%)

```sql
SELECT instance, value FROM victoriametrics.oscar_node_iowait_pct
WHERE time = 'now'
  AND value = '> 20';

-- PromQL: oscar:node:iowait_pct > 20
```

---

## OSCAR Anomaly Detection — Band Metrics

> **Source:** `oscar/oscar-metricstore/vmalert/rules/recording-anomaly-adaptive.on.yml`
>
> Anomaly bands are computed automatically for all 8 OSCAR node metrics.
> The algorithm runs every minute and produces three queryable metrics:
>
> | Metric | Description |
> |--------|-------------|
> | `anomaly:level` | Actual current value of the monitored metric |
> | `anomaly:upper_band` | Dynamic upper threshold (mean + 2σ, 26h window + seasonality) |
> | `anomaly:lower_band` | Dynamic lower threshold (mean − 2σ, clamped to 0) |
>
> **Note:** Bands stabilise after ~24-26 hours of data. Before that, bands will be wider than expected.

### Anomaly Name Reference

Every anomaly metric carries an `anomaly_name` label. These are the exact values set by the input tagging rules:

| `anomaly_name` | Maps to | Strategy |
|---|---|---|
| `oscar_node_cpu` | `oscar:node:cpu_utilization` | adaptive |
| `oscar_node_memory` | `oscar:node:memory_utilization` | adaptive |
| `oscar_node_swap` | `oscar:node:swap_utilization` | robust |
| `oscar_node_disk` | `oscar:node:disk_utilization` | robust |
| `oscar_node_iowait` | `oscar:node:iowait_pct` | adaptive |
| `oscar_node_load_per_cpu` | `oscar:node:load_per_cpu` | adaptive |
| `oscar_node_network_rx` | `oscar:node:network_rx_bytes_rate` | adaptive |
| `oscar_node_network_tx` | `oscar:node:network_tx_bytes_rate` | adaptive |

**Additional labels on all anomaly metrics:** `instance`, `job`, `anomaly_type="resource"`, `anomaly_strategy`

---

### Anomaly Query Examples

#### All metrics currently outside their anomaly band

```sql
SELECT instance, anomaly_name, value FROM victoriametrics.instant
WHERE metric = 'anomaly:level'
  AND anomaly_strategy = 'adaptive';

-- PromQL: anomaly:level{anomaly_strategy="adaptive"}
-- Returns one row per (instance × signal) currently tracked
```

#### Current upper band — all signals, all servers

```sql
SELECT instance, anomaly_name, value FROM victoriametrics.instant
WHERE metric = 'anomaly:upper_band'
  AND anomaly_strategy = 'adaptive';

-- PromQL: anomaly:upper_band{anomaly_strategy="adaptive"}
```

#### Current lower band — all signals, all servers

```sql
SELECT instance, anomaly_name, value FROM victoriametrics.instant
WHERE metric = 'anomaly:lower_band'
  AND anomaly_strategy = 'adaptive';
```

#### CPU anomaly band — current snapshot for all servers

```sql
SELECT instance, value FROM victoriametrics.instant
WHERE metric = 'anomaly:upper_band'
  AND anomaly_name = 'oscar_node_cpu';

-- PromQL: anomaly:upper_band{anomaly_name="oscar_node_cpu"}
```

#### Memory anomaly — level + upper + lower for one server

```sql
-- Level (actual value)
SELECT instance, value FROM victoriametrics.instant
WHERE metric = 'anomaly:level'
  AND anomaly_name = 'oscar_node_memory'
  AND instance = 'server01:9100';

-- Upper band
SELECT instance, value FROM victoriametrics.instant
WHERE metric = 'anomaly:upper_band'
  AND anomaly_name = 'oscar_node_memory'
  AND instance = 'server01:9100';

-- Lower band
SELECT instance, value FROM victoriametrics.instant
WHERE metric = 'anomaly:lower_band'
  AND anomaly_name = 'oscar_node_memory'
  AND instance = 'server01:9100';
```

#### CPU anomaly band trend — last 12h, 5-minute resolution

```sql
SELECT instance, timestamp, value FROM victoriametrics.range_query
WHERE metric = 'anomaly:upper_band'
  AND anomaly_name = 'oscar_node_cpu'
  AND time_start = 'now-12h'
  AND time_end = 'now'
  AND step = '5m';
```

#### Memory anomaly band trend — last 24h

```sql
SELECT instance, timestamp, value FROM victoriametrics.range_query
WHERE metric = 'anomaly:upper_band'
  AND anomaly_name = 'oscar_node_memory'
  AND time_start = 'now-24h'
  AND time_end = 'now'
  AND step = '15m';
```

#### Disk anomaly bands — last 6h (robust strategy)

```sql
SELECT instance, timestamp, value FROM victoriametrics.range_query
WHERE metric = 'anomaly:upper_band'
  AND anomaly_name = 'oscar_node_disk'
  AND time_start = 'now-6h'
  AND step = '5m';
```

#### Network RX anomaly — show level AND upper band side by side (two queries)

```sql
-- Actual traffic rate
SELECT instance, timestamp, value FROM victoriametrics.range_query
WHERE metric = 'anomaly:level'
  AND anomaly_name = 'oscar_node_network_rx'
  AND time_start = 'now-6h'
  AND step = '5m';

-- Upper band (threshold)
SELECT instance, timestamp, value FROM victoriametrics.range_query
WHERE metric = 'anomaly:upper_band'
  AND anomaly_name = 'oscar_node_network_rx'
  AND time_start = 'now-6h'
  AND step = '5m';
```

#### Filter anomaly bands to a specific server

```sql
SELECT anomaly_name, value FROM victoriametrics.instant
WHERE metric = 'anomaly:upper_band'
  AND instance = 'server01:9100'
  AND anomaly_strategy = 'adaptive';

-- PromQL: anomaly:upper_band{instance="server01:9100", anomaly_strategy="adaptive"}
-- Returns all 8 signals' upper bands for that one server
```

#### Load anomaly trend — last 6h

```sql
SELECT instance, timestamp, value FROM victoriametrics.range_query
WHERE metric = 'anomaly:level'
  AND anomaly_name = 'oscar_node_load_per_cpu'
  AND time_start = 'now-6h'
  AND step = '1m';
```

#### I/O wait anomaly — all servers, current snapshot

```sql
SELECT instance, value FROM victoriametrics.instant
WHERE metric = 'anomaly:level'
  AND anomaly_name = 'oscar_node_iowait';
```

---

## OSCAR Recording Rules — Platform & Container Metrics

> **File:** `oscar-conf/metricstore/vmalert/rules/recording-rules.oscar.yml`
>
> These pre-computed metrics cover platform health, container metrics, and reachability.
> Access via the generic `instant` / `range_query` tables with `WHERE metric = '...'`.

### Available Curated Metrics

| Metric | Description | Labels |
|--------|-------------|--------|
| `oscar:host:cpu_usage_percent` | Host CPU usage % (non-idle) | `instance` |
| `oscar:host:cpu_iowait_percent` | Host CPU iowait % | `instance` |
| `oscar:host:memory_usage_percent` | Host memory usage % (excl. buffers/cache) | `instance` |
| `oscar:host:memory_available_bytes` | Host available memory in bytes | `instance` |
| `oscar:host:swap_usage_percent` | Host swap usage % | `instance` |
| `oscar:host:disk_usage_percent` | Host disk usage % per mountpoint | `instance`, `mountpoint` |
| `oscar:host:inode_usage_percent` | Host inode usage % per mountpoint | `instance`, `mountpoint` |
| `oscar:host:reachable` | Host reachability via ICMP (1=up, 0=down) | `instance` |
| `oscar:host:packet_loss_percent` | Host packet loss % | `instance` |
| `oscar:container:cpu_usage_percent` | Container CPU usage % | `instance`, `name` |
| `oscar:container:memory_usage_percent` | Container memory usage % of limit | `instance`, `name` |
| `oscar:container:volume_inode_usage_percent` | Container volume inode usage % | `instance` |
| `oscar:container:cpu_throttle_rate` | Container CPU throttle rate | `instance`, `name` |
| `oscar:platform:health` | OSCAR platform health (1=healthy, 0=unhealthy) | `instance` |
| `oscar:platform:process_up` | Critical process status (1=up, 0=down) | `instance`, `process_pattern` |
| `oscar:platform:mount_status` | NFS mount status (1=mounted, 0=missing) | `instance` |
| `oscar:platform:duplicate_processes` | Duplicate process detected (1=found) | `instance`, `process_pattern` |
| `oscar:vmdb:ingestion_rate` | VictoriaMetrics ingestion rate (rows/sec) | `instance` |
| `oscar:vmdb:disk_usage_percent` | VictoriaMetrics disk usage % | `instance` |
| `oscar:vmdb:http_error_rate` | VictoriaMetrics HTTP error rate | `instance`, `path` |

---

### Platform Metric Query Examples

#### Hosts that are currently unreachable

```sql
SELECT * FROM victoriametrics.instant
WHERE metric = 'oscar:host:reachable'
  AND value = '== 0';

-- PromQL: oscar:host:reachable == 0
```

#### Down processes — any critical process not running

```sql
SELECT * FROM victoriametrics.instant
WHERE metric = 'oscar:platform:process_up'
  AND value = '== 0';

-- PromQL: oscar:platform:process_up == 0
```

#### OSCAR platform health — is OSCAR healthy?

```sql
SELECT * FROM victoriametrics.instant
WHERE metric = 'oscar:platform:health';

-- PromQL: oscar:platform:health  (1 = healthy, 0 = unhealthy)
```

#### Container CPU usage — top containers right now

```sql
SELECT * FROM victoriametrics.instant
WHERE metric = 'oscar:container:cpu_usage_percent'
  AND value = '> 10';

-- PromQL: oscar:container:cpu_usage_percent > 10
```

#### VMDB ingestion rate — last hour trend

```sql
SELECT * FROM victoriametrics.range_query
WHERE metric = 'oscar:vmdb:ingestion_rate'
  AND time_start = 'now-1h'
  AND time_end = 'now'
  AND step = '1m';

-- PromQL: oscar:vmdb:ingestion_rate  (start=now-1h, end=now, step=1m)
```

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `Syntax error: expected symbol '[Identifier]' near start` | `start`, `end`, `range`, or `window` are SQL reserved words | Use `time_start`/`time_end`, `fn_window`, and `range_query` table name |
| `WHERE metric = '<name>' is required` | No `metric` condition on `instant`/`range_query` | Always include `AND metric = 'your_metric'`, or use a named table (e.g. `oscar_node_cpu_utilization`) |
| `Range queries require: WHERE time_start = ...` | *(No longer thrown — defaults to last 1 hour)* | — |
| `Binder Error: Referenced column "job" not found` | Metric doesn't have that label | Use `SELECT *` or run `SELECT * FROM victoriametrics.labels WHERE metric = '...'` first |
| Empty result set | Metric or labels don't match | Use `labels` table to discover valid label values |
| Empty result on `anomaly:*` metrics | No node_exporter data in vmdb | Check vmagent has node-exporter targets; inject synthetic data for local dev (see pipeline doc §11) |
| Wrong `anomaly_name` value | Using wrong label value | Use exact names: `oscar_node_cpu`, `oscar_node_memory`, `oscar_node_swap`, `oscar_node_disk`, `oscar_node_iowait`, `oscar_node_load_per_cpu`, `oscar_node_network_rx`, `oscar_node_network_tx` |
| `Cannot reach http://vmdb:8428` | VictoriaMetrics unreachable | Check `vmdb` container is running: `docker ps \| grep vmdb` |
