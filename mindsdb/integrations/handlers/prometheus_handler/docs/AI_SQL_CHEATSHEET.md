# OSCAR Kore — VictoriaMetrics SQL Cheat Sheet

> **Purpose:** Reference for manual queries + NL→SQL agent training.
> All queries run against the `victoriametrics` datasource in Kore (MindsDB).
> Engine: `prometheus` handler → VictoriaMetrics at `http://vmdb:8428`
>
> **This is one of two cheatsheets. See also: `OSCAR_DB_CHEATSHEET.md` for MySQL operational data.**
>
> **Datasource routing:**
> - Use `victoriametrics` → current metric values, trends, rates, anomaly z-scores, queue depths
> - Use `oscar_db` → alert history, task run details/errors, notification delivery audit, server inventory, LLM spend

---

## Quick Reference — All 25 Tables

### Infrastructure Node Metrics (named tables — no `WHERE metric` needed)

| Table | What it measures | Unit | Extra columns |
|-------|-----------------|------|---------------|
| `infra_node_cpu_utilization` | CPU usage % | 0–100 | — |
| `infra_node_memory_utilization` | Memory usage % (excl. buffers/cache) | 0–100 | — |
| `infra_node_swap_utilization` | Swap usage % | 0–100 | — |
| `infra_node_disk_utilization` | Disk usage % per mountpoint | 0–100 | `mountpoint` |
| `infra_node_iowait_pct` | I/O wait % (CPU time waiting on disk) | 0–100 | — |
| `infra_node_load_per_cpu` | Load average ÷ vCPU count | ratio | — |
| `infra_node_network_rx_bytes_rate` | Network receive bytes/s | bytes/s | — |
| `infra_node_network_tx_bytes_rate` | Network transmit bytes/s | bytes/s | — |

### Anomaly Detection (named tables)

| Table | What it measures | Key label |
|-------|-----------------|-----------|
| `anomaly_zscore` | Z-score: how many σ from baseline. Alert threshold: ±2 major, ±3 critical | `anomaly_name` |
| `anomaly_level` | Actual metric value (with anomaly labels attached) | `anomaly_name` |
| `anomaly_upper_band` | Adaptive upper band = mean + 2σ (Grafana visualisation) | `anomaly_name` |
| `anomaly_lower_band` | Adaptive lower band = mean − 2σ (Grafana visualisation) | `anomaly_name` |

`anomaly_name` values: `cpu_utilization` `memory_utilization` `swap_utilization` `disk_utilization` `iowait_pct` `load_per_cpu` `network_rx_bytes_rate` `network_tx_bytes_rate`

### OSCAR Platform Operational Metrics (named tables — live Gauges/Counters)

| Table | What it measures | Key column(s) | Value meaning |
|-------|-----------------|---------------|---------------|
| `oscar_alert_active_tasks` | Active in-flight notification dispatch tasks | — (scalar) | count of active tasks |
| `oscar_alert_queue_depth` | Live alert queue backlog | `queue` | pending alert count |
| `oscar_alert_processed` | Alerts processed (cumulative counter) | `status` | total count by status (success/error) |
| `oscar_alert_circuit_breaker` | Celery taskmanager circuit breaker state | `datacenter`, `environment` | 0=closed (healthy), 1=open (tripped) |
| `oscar_task_history` | Per-task execution results (80+ series) | `task_name`, `task_type`, `state` | 1=latest result; filter by `state` |
| `oscar_task_rate` | Task execution rate (5-min avg) | — | tasks/sec |
| `oscar_task_workers` | Active Celery worker count | — | worker count |
| `oscar_notifier_failed` | Failed notification deliveries | `notifier_name`, `notifier_type`, `error_type`, `provider` | cumulative failure count |
| `oscar_topic_classifier_health` | Topic classifier AI backend health | `datacenter`, `environment` | 1=healthy, 0=unhealthy |

### Generic Tables (any PromQL metric)

| Table | Use |
|-------|-----|
| `instant` | Current snapshot of any metric. Requires `WHERE metric = '...'` |
| `range_query` | Time-series history of any metric. Requires `WHERE metric = '...'` |
| `metrics` | List all metric names in VictoriaMetrics |
| `labels` | List all label combinations for a metric |

---

## Columns Available on Every Named Table

### Infra node tables
```
metric, value, timestamp, instance, job, meta_hostname, meta_ipaddress,
datacenter, environment, oscar_metric_type, mountpoint
```
> `mountpoint` is populated only for `infra_node_disk_utilization`, NULL for others.

### Anomaly tables
```
metric, value, timestamp, instance, job, meta_hostname, meta_ipaddress,
datacenter, environment, anomaly_name, anomaly_strategy, anomaly_type, anomaly_method
```

### OSCAR platform operational tables
```
metric, value, timestamp, job, instance
+ table-specific columns:
  oscar_alert_active_tasks     → (no extra columns — scalar Gauge)
  oscar_alert_queue_depth      → queue
  oscar_alert_processed        → status
  oscar_alert_circuit_breaker  → datacenter, environment
  oscar_task_history           → task_name, task_type, state,
                                  meta_hostname, meta_component, meta_datacenter
  oscar_task_rate              → (no extra columns)
  oscar_task_workers           → (no extra columns)
  oscar_notifier_failed        → notifier_name, notifier_type, namespace, error_type, provider
  oscar_topic_classifier_health→ datacenter, environment
```

---

## Time Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `time = 'now'` | Instant query — current value | `WHERE time = 'now'` |
| `time_start` + `time_end` | Range query — time window | `time_start = 'now-6h' AND time_end = 'now'` |
| `step` | Sample resolution (default `1m`) | `step = '5m'` |
| *(nothing)* | Default: range query, last 1h, 1m step | |

**Relative time syntax:** `now-15m` `now-1h` `now-6h` `now-24h` `now-7d`

---

## WHERE Filters

| SQL | Meaning | PromQL produced |
|-----|---------|----------------|
| `meta_hostname = 'server01'` | Exact hostname match | `meta_hostname="server01"` |
| `meta_hostname LIKE '%web%'` | Hostname contains "web" | `meta_hostname=~".*web.*"` |
| `meta_hostname LIKE 'dc1-%'` | Hostname starts with "dc1-" | `meta_hostname=~"dc1\-.*"` |
| `meta_ipaddress = '10.0.1.55'` | Exact IP | `meta_ipaddress="10.0.1.55"` |
| `datacenter = 'dc1'` | Filter by datacenter | `datacenter="dc1"` |
| `environment = 'production'` | Filter by environment | `environment="production"` |
| `value = '> 80'` | Threshold — only rows where value > 80 | appends `> 80` to PromQL |
| `value = '< 0.5'` | Threshold — only rows where value < 0.5 | appends `< 0.5` to PromQL |

---

## ── SECTION 1: Current Snapshot Queries ────────────────────────────────────

### All servers — CPU right now
```sql
SELECT meta_hostname, value
FROM victoriametrics.infra_node_cpu_utilization
WHERE time = 'now';
```

### All servers — all 8 metrics right now (run separately per metric)
```sql
SELECT meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization    WHERE time = 'now';
SELECT meta_hostname, value FROM victoriametrics.infra_node_memory_utilization WHERE time = 'now';
SELECT meta_hostname, value FROM victoriametrics.infra_node_swap_utilization   WHERE time = 'now';
SELECT meta_hostname, value FROM victoriametrics.infra_node_iowait_pct         WHERE time = 'now';
SELECT meta_hostname, value FROM victoriametrics.infra_node_load_per_cpu       WHERE time = 'now';
SELECT meta_hostname, value FROM victoriametrics.infra_node_network_rx_bytes_rate WHERE time = 'now';
SELECT meta_hostname, value FROM victoriametrics.infra_node_network_tx_bytes_rate WHERE time = 'now';
```

### Disk — all mountpoints right now
```sql
SELECT meta_hostname, mountpoint, value
FROM victoriametrics.infra_node_disk_utilization
WHERE time = 'now';
```

### One specific server — full health snapshot
```sql
-- Replace 'dc1-dev-web-01' with the target hostname
SELECT meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization    WHERE meta_hostname = 'dc1-dev-web-01' AND time = 'now';
SELECT meta_hostname, value FROM victoriametrics.infra_node_memory_utilization WHERE meta_hostname = 'dc1-dev-web-01' AND time = 'now';
SELECT meta_hostname, mountpoint, value FROM victoriametrics.infra_node_disk_utilization WHERE meta_hostname = 'dc1-dev-web-01' AND time = 'now';
SELECT meta_hostname, value FROM victoriametrics.infra_node_load_per_cpu       WHERE meta_hostname = 'dc1-dev-web-01' AND time = 'now';
SELECT meta_hostname, value FROM victoriametrics.infra_node_iowait_pct         WHERE meta_hostname = 'dc1-dev-web-01' AND time = 'now';
SELECT meta_hostname, value FROM victoriametrics.infra_node_network_rx_bytes_rate WHERE meta_hostname = 'dc1-dev-web-01' AND time = 'now';
SELECT meta_hostname, value FROM victoriametrics.infra_node_network_tx_bytes_rate WHERE meta_hostname = 'dc1-dev-web-01' AND time = 'now';
```

---

## ── SECTION 2: Threshold / Alert Queries ───────────────────────────────────

### Servers with CPU above 80%
```sql
SELECT meta_hostname, value
FROM victoriametrics.infra_node_cpu_utilization
WHERE time = 'now'
  AND value = '> 80';
```

### Servers with memory above 90%
```sql
SELECT meta_hostname, value
FROM victoriametrics.infra_node_memory_utilization
WHERE time = 'now'
  AND value = '> 90';
```

### Servers actively using swap (swap > 0%)
```sql
SELECT meta_hostname, value
FROM victoriametrics.infra_node_swap_utilization
WHERE time = 'now'
  AND value = '> 0';
```

### Disk partitions above 70% used
```sql
SELECT meta_hostname, mountpoint, value
FROM victoriametrics.infra_node_disk_utilization
WHERE time = 'now'
  AND value = '> 70';
```

### Disk partitions critically full (above 90%)
```sql
SELECT meta_hostname, mountpoint, value
FROM victoriametrics.infra_node_disk_utilization
WHERE time = 'now'
  AND value = '> 90';
```

### CPU saturated servers (load per CPU > 1.0 means overloaded)
```sql
SELECT meta_hostname, value
FROM victoriametrics.infra_node_load_per_cpu
WHERE time = 'now'
  AND value = '> 1';
```

### High I/O wait — disk bottleneck (above 20%)
```sql
SELECT meta_hostname, value
FROM victoriametrics.infra_node_iowait_pct
WHERE time = 'now'
  AND value = '> 20';
```

---

## ── SECTION 3: Historical Trend Queries ────────────────────────────────────

### CPU trend — last 1 hour, 1-minute resolution
```sql
SELECT timestamp, meta_hostname, value
FROM victoriametrics.infra_node_cpu_utilization
WHERE time_start = 'now-1h'
  AND time_end = 'now'
  AND step = '1m';
```

### Memory trend — last 24 hours, 15-minute resolution
```sql
SELECT timestamp, meta_hostname, value
FROM victoriametrics.infra_node_memory_utilization
WHERE time_start = 'now-24h'
  AND time_end = 'now'
  AND step = '15m';
```

### CPU trend for one server — last 6 hours
```sql
SELECT timestamp, value
FROM victoriametrics.infra_node_cpu_utilization
WHERE meta_hostname = 'dc1-dev-web-01'
  AND time_start = 'now-6h'
  AND step = '5m';
```

### Disk trend for root partition — last 7 days (capacity planning)
```sql
SELECT timestamp, meta_hostname, value
FROM victoriametrics.infra_node_disk_utilization
WHERE mountpoint = '/'
  AND time_start = 'now-7d'
  AND step = '1h';
```

### Network traffic trend — last 2 hours
```sql
SELECT timestamp, meta_hostname, value
FROM victoriametrics.infra_node_network_rx_bytes_rate
WHERE time_start = 'now-2h'
  AND step = '1m';

SELECT timestamp, meta_hostname, value
FROM victoriametrics.infra_node_network_tx_bytes_rate
WHERE time_start = 'now-2h'
  AND step = '1m';
```

---

## ── SECTION 4: Filter by Group / Environment ────────────────────────────────

### All servers in datacenter dc1
```sql
SELECT meta_hostname, value
FROM victoriametrics.infra_node_cpu_utilization
WHERE datacenter = 'dc1'
  AND time = 'now';
```

### All production servers — memory snapshot
```sql
SELECT meta_hostname, value
FROM victoriametrics.infra_node_memory_utilization
WHERE environment = 'production'
  AND time = 'now';
```

### All web servers (hostname contains "web")
```sql
SELECT meta_hostname, value
FROM victoriametrics.infra_node_cpu_utilization
WHERE meta_hostname LIKE '%web%'
  AND time = 'now';
```

### All servers except test machines
```sql
SELECT meta_hostname, value
FROM victoriametrics.infra_node_cpu_utilization
WHERE meta_hostname NOT LIKE '%test%'
  AND time = 'now';
```

---

## ── SECTION 5: Anomaly Detection Queries ───────────────────────────────────

### All current anomalies — any server, any metric (|z| ≥ 2)
```sql
-- Major anomalies (positive spikes)
SELECT meta_hostname, anomaly_name, value
FROM victoriametrics.anomaly_zscore
WHERE value = '> 2'
  AND time = 'now';

-- Major anomalies (negative drops)
SELECT meta_hostname, anomaly_name, value
FROM victoriametrics.anomaly_zscore
WHERE value = '< -2'
  AND time = 'now';
```

### Critical anomalies only (|z| ≥ 3)
```sql
SELECT meta_hostname, anomaly_name, value
FROM victoriametrics.anomaly_zscore
WHERE value = '>= 3'
  AND time = 'now';

SELECT meta_hostname, anomaly_name, value
FROM victoriametrics.anomaly_zscore
WHERE value = '<= -3'
  AND time = 'now';
```

### All z-scores for one server right now
```sql
SELECT anomaly_name, value
FROM victoriametrics.anomaly_zscore
WHERE meta_hostname = 'dc1-dev-web-01'
  AND time = 'now';
```

### Z-score trend for CPU — last 2 hours
```sql
SELECT timestamp, meta_hostname, value
FROM victoriametrics.anomaly_zscore
WHERE anomaly_name = 'cpu_utilization'
  AND time_start = 'now-2h'
  AND step = '1m';
```

### Z-score trend for one server, one metric
```sql
SELECT timestamp, value
FROM victoriametrics.anomaly_zscore
WHERE anomaly_name = 'memory_utilization'
  AND meta_hostname = 'dc1-dev-web-01'
  AND time_start = 'now-6h'
  AND step = '5m';
```

### Adaptive bands for one server — memory (Grafana context)
```sql
-- Actual value
SELECT timestamp, value FROM victoriametrics.anomaly_level
WHERE anomaly_name = 'memory_utilization'
  AND meta_hostname = 'dc1-dev-web-01'
  AND time_start = 'now-6h' AND step = '5m';

-- Upper band
SELECT timestamp, value FROM victoriametrics.anomaly_upper_band
WHERE anomaly_name = 'memory_utilization'
  AND meta_hostname = 'dc1-dev-web-01'
  AND time_start = 'now-6h' AND step = '5m';

-- Lower band
SELECT timestamp, value FROM victoriametrics.anomaly_lower_band
WHERE anomaly_name = 'memory_utilization'
  AND meta_hostname = 'dc1-dev-web-01'
  AND time_start = 'now-6h' AND step = '5m';
```

### All current anomaly bands — which metrics have wide bands (high volatility)
```sql
SELECT meta_hostname, anomaly_name, value
FROM victoriametrics.anomaly_upper_band
WHERE time = 'now';
```

---

## ── SECTION 6: Generic Tables (any PromQL metric) ──────────────────────────

### Discover what metrics exist
```sql
SELECT * FROM victoriametrics.metrics LIMIT 50;
```

### Discover labels on a specific metric
```sql
SELECT * FROM victoriametrics.labels
WHERE metric = 'infra:node:cpu_utilization'
LIMIT 20;
```

### Query any metric directly via instant table
```sql
SELECT metric, instance, value
FROM victoriametrics.instant
WHERE metric = 'infra:node:cpu_utilization'
  AND time = 'now';
```

### Query with PromQL function via instant table
```sql
-- Rate of network bytes received over 5m window
SELECT instance, value FROM victoriametrics.instant
WHERE metric = 'node_network_receive_bytes_total'
  AND fn = 'rate'
  AND fn_window = '5m'
  AND time = 'now';

-- Sum CPU idle rate by job
SELECT job, value FROM victoriametrics.instant
WHERE metric = 'node_cpu_seconds_total'
  AND fn = 'sum:rate'
  AND fn_labels = 'job'
  AND fn_window = '5m'
  AND time = 'now';
```

---

## ── SECTION 7: NL → SQL Patterns (for Agent Training) ──────────────────────

These show how natural language maps to SQL — the agent will use these.

| User says | SQL pattern |
|-----------|-------------|
| "What is the CPU on server X?" | `infra_node_cpu_utilization WHERE meta_hostname = 'X' AND time = 'now'` |
| "How is server X doing?" | Run all 8 infra tables with `meta_hostname = 'X' AND time = 'now'` |
| "Which servers have high CPU?" | `infra_node_cpu_utilization WHERE time = 'now' AND value = '> 80'` |
| "Is any disk full?" | `infra_node_disk_utilization WHERE time = 'now' AND value = '> 90'` |
| "Show me memory trend for X over last 6h" | `infra_node_memory_utilization WHERE meta_hostname = 'X' AND time_start = 'now-6h' AND step = '5m'` |
| "Is anything anomalous right now?" | `anomaly_zscore WHERE (value = '> 2' OR value = '< -2') AND time = 'now'` |
| "What's wrong with server X?" | z-score: `anomaly_zscore WHERE meta_hostname = 'X' AND time = 'now'` + all infra tables for X |
| "Which servers are overloaded?" | `infra_node_load_per_cpu WHERE time = 'now' AND value = '> 1'` |
| "Is there a network spike anywhere?" | `infra_node_network_rx_bytes_rate WHERE time = 'now'` + check against upper band |
| "Show anomaly history for X last 2h" | `anomaly_zscore WHERE meta_hostname = 'X' AND time_start = 'now-2h' AND step = '1m'` |
| "Compare CPU across all web servers" | `infra_node_cpu_utilization WHERE meta_hostname LIKE '%web%' AND time = 'now'` |
| "Are the prod servers healthy?" | All 8 infra tables with `environment = 'production' AND time = 'now'` |

---

## ── SECTION 8: Interpreting Values ─────────────────────────────────────────

| Metric | Normal range | Warning | Critical | Notes |
|--------|-------------|---------|----------|-------|
| `cpu_utilization` | 0–70% | >80% | >95% | Sustained high is bad; spikes OK |
| `memory_utilization` | 0–80% | >85% | >95% | excludes buffers/cache |
| `swap_utilization` | 0% | >10% | >50% | Any swap usage is a warning sign |
| `disk_utilization` | 0–70% | >80% | >90% | Per mountpoint — check `/` especially |
| `iowait_pct` | 0–5% | >15% | >30% | High = disk bottleneck |
| `load_per_cpu` | 0–0.7 | >1.0 | >2.0 | > 1.0 = CPU saturation |
| `network_rx/tx_bytes_rate` | baseline | 2× baseline | 5× baseline | Compare against upper_band |
| `anomaly_zscore` | -1 to +1 | ±2 | ±3 | Z-score — σ from 1h mean |

---

## ── SECTION 9: OSCAR Platform Health Queries ────────────────────────────────

### Alert pipeline — active notification tasks right now (is dispatch working?)
```sql
SELECT value
FROM victoriametrics.oscar_alert_active_tasks
WHERE time = 'now';
```

### Alert pipeline — active task trend (is dispatch stalling?)
```sql
SELECT timestamp, value
FROM victoriametrics.oscar_alert_active_tasks
WHERE time_start = 'now-1h' AND step = '1m';
```

### Alert pipeline — current queue depths per queue
```sql
SELECT queue, value
FROM victoriametrics.oscar_alert_queue_depth
WHERE time = 'now';
```

### Alert pipeline — is there a backlog?
```sql
SELECT queue, value
FROM victoriametrics.oscar_alert_queue_depth
WHERE value = '> 0' AND time = 'now';
```

### Alert pipeline — queue depth trend (is tm_alerts growing?)
```sql
SELECT timestamp, queue, value
FROM victoriametrics.oscar_alert_queue_depth
WHERE queue = 'tm_alerts'
  AND time_start = 'now-30m' AND step = '1m';
```

### Alert pipeline — total alerts processed (success vs error)
```sql
SELECT status, value
FROM victoriametrics.oscar_alert_processed
WHERE time = 'now';
```

### Circuit breaker — is the taskmanager circuit breaker open?
```sql
SELECT datacenter, environment, value
FROM victoriametrics.oscar_alert_circuit_breaker
WHERE time = 'now';
```

### Circuit breaker — has it been tripping? (trend)
```sql
SELECT timestamp, value
FROM victoriametrics.oscar_alert_circuit_breaker
WHERE time_start = 'now-6h' AND step = '1m';
```

### Tasks — which tasks are failing right now?
```sql
SELECT task_name, task_type, state, meta_hostname, meta_component
FROM victoriametrics.oscar_task_history
WHERE state = 'FAILURE' AND time = 'now';
```

### Tasks — all task results right now (SUCCESS + FAILURE)
```sql
SELECT task_name, task_type, state, meta_hostname, value
FROM victoriametrics.oscar_task_history
WHERE time = 'now';
```

### Tasks — failures for a specific task type
```sql
SELECT task_name, meta_hostname, state
FROM victoriametrics.oscar_task_history
WHERE task_type = 'fabric' AND state = 'FAILURE' AND time = 'now';
```

### Tasks — failures in a specific datacenter
```sql
SELECT task_name, meta_hostname, state
FROM victoriametrics.oscar_task_history
WHERE meta_datacenter = 'dc1' AND state = 'FAILURE' AND time = 'now';
```

### Tasks — current execution rate (tasks/sec)
```sql
SELECT value
FROM victoriametrics.oscar_task_rate
WHERE time = 'now';
```

### Tasks — how many workers are active?
```sql
SELECT value
FROM victoriametrics.oscar_task_workers
WHERE time = 'now';
```

### Tasks — worker count trend (did workers drop?)
```sql
SELECT timestamp, value
FROM victoriametrics.oscar_task_workers
WHERE time_start = 'now-2h' AND step = '1m';
```

### Notifier — which notifiers are failing and why?
```sql
SELECT notifier_name, notifier_type, error_type, provider, value
FROM victoriametrics.oscar_notifier_failed
WHERE time = 'now';
```

### Notifier — failure count by error type
```sql
SELECT error_type, value
FROM victoriametrics.oscar_notifier_failed
WHERE time = 'now';
```

### Notifier — failure trend for autocaller
```sql
SELECT timestamp, value
FROM victoriametrics.oscar_notifier_failed
WHERE notifier_name = 'autocaller'
  AND time_start = 'now-6h' AND step = '5m';
```

### Topic classifier — is the AI backend healthy?
```sql
SELECT datacenter, environment, value
FROM victoriametrics.oscar_topic_classifier_health
WHERE time = 'now';
```

### Topic classifier — health history (did it go down?)
```sql
SELECT timestamp, value
FROM victoriametrics.oscar_topic_classifier_health
WHERE time_start = 'now-6h' AND step = '1m';
```

---

## ── SECTION 10: NL → SQL Patterns — OSCAR Platform ─────────────────────────

| User says | SQL pattern |
|-----------|-------------|
| "Is the alert pipeline working?" | `oscar_alert_active_tasks WHERE time = 'now'` + `oscar_alert_queue_depth WHERE time = 'now'` |
| "Is there an alert backlog?" | `oscar_alert_queue_depth WHERE value = '> 0' AND time = 'now'` |
| "How many alerts have been processed?" | `oscar_alert_processed WHERE time = 'now'` |
| "Is the circuit breaker open?" | `oscar_alert_circuit_breaker WHERE time = 'now'` (value 1 = open/tripped) |
| "Which tasks are failing?" | `oscar_task_history WHERE state = 'FAILURE' AND time = 'now'` |
| "Are fabric tasks failing?" | `oscar_task_history WHERE task_type = 'fabric' AND state = 'FAILURE' AND time = 'now'` |
| "What tasks failed on host X?" | `oscar_task_history WHERE meta_hostname = 'X' AND state = 'FAILURE' AND time = 'now'` |
| "How fast are tasks running?" | `oscar_task_rate WHERE time = 'now'` |
| "How many workers are running?" | `oscar_task_workers WHERE time = 'now'` |
| "Are notifications failing?" | `oscar_notifier_failed WHERE time = 'now'` |
| "Why is autocaller failing?" | `oscar_notifier_failed WHERE notifier_name = 'autocaller' AND time = 'now'` |
| "Is the topic classifier up?" | `oscar_topic_classifier_health WHERE time = 'now'` (value 1 = healthy) |
| "Did something break in the last hour?" | Circuit breaker trend + task failure + notifier failures — all `time_start = 'now-1h'` |
| "Give me a full OSCAR health check" | Active tasks + queue depth + circuit breaker + task failures + notifier fails + classifier health — all `time = 'now'` |
