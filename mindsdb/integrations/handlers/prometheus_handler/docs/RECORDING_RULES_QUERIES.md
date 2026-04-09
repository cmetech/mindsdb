# MindsDB Queries — OSCAR Recording Rules

Reference for querying OSCAR pre-computed recording rule metrics via the `victoriametrics` MindsDB datasource.

---

## Setup

```sql
CREATE DATABASE victoriametrics
WITH ENGINE = 'prometheus',
PARAMETERS = {
  "host": "http://vmdb:8428",
  "timeout": 30
};
```

---

## Server Identity Labels

Each node-exporter target carries three labels set by vmagent during scraping:

| Label | Contains | Example | Use in WHERE |
|-------|----------|---------|--------------|
| `instance` | `ip:port` | `192.168.1.100:9100` | Exact or regex match |
| `meta_hostname` | FQDN / hostname | `db01.prod.example.com` | Exact or regex match |
| `meta_ipaddress` | IP only (no port) | `192.168.1.100` | Exact or regex match |

Use whichever the agent supplies — the user may say "server01" (hostname) or "10.0.0.5" (IP).

---

## Querying a Specific Server — Real-Time

These are the patterns an agent should generate when a user asks about one or two named servers.

### By hostname

```sql
-- "What is the current CPU on db01.prod.example.com?"
SELECT instance, meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization
WHERE meta_hostname = 'db01.prod.example.com'
  AND time = 'now';

-- Partial hostname match (regex)
SELECT instance, meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization
WHERE meta_hostname LIKE 'db01.*'
  AND time = 'now';
```

### By IP address

```sql
-- "Show me memory on 10.0.1.55"
SELECT instance, meta_hostname, value FROM victoriametrics.infra_node_memory_utilization
WHERE meta_ipaddress = '10.0.1.55'
  AND time = 'now';
```

### By instance (IP:port — the internal PromQL label)

```sql
SELECT instance, value FROM victoriametrics.infra_node_cpu_utilization
WHERE instance = '10.0.1.55:9100'
  AND time = 'now';
```

### Two specific servers side-by-side

Because SQL `OR` is not supported in the WHERE clause, run two queries — one per server:

```sql
-- Server 1
SELECT instance, meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization
WHERE meta_hostname = 'web01.prod.example.com'
  AND time = 'now';

-- Server 2
SELECT instance, meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization
WHERE meta_hostname = 'web02.prod.example.com'
  AND time = 'now';
```

### All metrics for one server — current snapshot

```sql
-- CPU
SELECT meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization
WHERE meta_hostname = 'db01.prod.example.com' AND time = 'now';

-- Memory
SELECT meta_hostname, value FROM victoriametrics.infra_node_memory_utilization
WHERE meta_hostname = 'db01.prod.example.com' AND time = 'now';

-- Disk (all mountpoints)
SELECT meta_hostname, mountpoint, value FROM victoriametrics.infra_node_disk_utilization
WHERE meta_hostname = 'db01.prod.example.com' AND time = 'now';

-- Load
SELECT meta_hostname, value FROM victoriametrics.infra_node_load_per_cpu
WHERE meta_hostname = 'db01.prod.example.com' AND time = 'now';

-- Network RX
SELECT meta_hostname, value FROM victoriametrics.infra_node_network_rx_bytes_rate
WHERE meta_hostname = 'db01.prod.example.com' AND time = 'now';
```

### Recent trend for one server

```sql
-- "How has CPU looked on db01 over the last 2 hours?"
SELECT timestamp, value FROM victoriametrics.infra_node_cpu_utilization
WHERE meta_hostname = 'db01.prod.example.com'
  AND time_start = 'now-2h'
  AND time_end = 'now'
  AND step = '1m';
```

---

## Infrastructure Node Metrics — Named Tables

Each recording rule has a dedicated SQL table. No `WHERE metric = ...` required.

| SQL Table | PromQL Metric | Key Labels |
|-----------|--------------|------------|
| `infra_node_cpu_utilization` | `infra:node:cpu_utilization` | `instance`, `meta_hostname`, `meta_ipaddress` |
| `infra_node_memory_utilization` | `infra:node:memory_utilization` | `instance`, `meta_hostname`, `meta_ipaddress` |
| `infra_node_swap_utilization` | `infra:node:swap_utilization` | `instance`, `meta_hostname`, `meta_ipaddress` |
| `infra_node_disk_utilization` | `infra:node:disk_utilization` | `instance`, `meta_hostname`, `meta_ipaddress`, `mountpoint` |
| `infra_node_iowait_pct` | `infra:node:iowait_pct` | `instance`, `meta_hostname`, `meta_ipaddress` |
| `infra_node_load_per_cpu` | `infra:node:load_per_cpu` | `instance`, `meta_hostname`, `meta_ipaddress` |
| `infra_node_network_rx_bytes_rate` | `infra:node:network_rx_bytes_rate` | `instance`, `meta_hostname`, `meta_ipaddress` |
| `infra_node_network_tx_bytes_rate` | `infra:node:network_tx_bytes_rate` | `instance`, `meta_hostname`, `meta_ipaddress` |

**Columns on every infra node table:** `metric`, `value`, `timestamp`, `instance`, `meta_hostname`, `meta_ipaddress`, `job`, `datacenter`, `environment`, `oscar_metric_type`, `mountpoint`

> `mountpoint` is populated only for `infra_node_disk_utilization`; it is `NULL` for all other tables.

**Default behaviour (no time params):** range query, last 1 hour, 1-minute step.

> **`value` in WHERE is a threshold filter**, not a column filter.
> `WHERE value = '> 80'` appends `> 80` to the PromQL expression.
> If no series exceed 80, you get an empty table — this is correct, not an error.

---

## Time Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `time` | Point-in-time instant query | — |
| `time_start` | Range start | `now-1h` |
| `time_end` | Range end | `now` |
| `step` | Sample interval | `1m` |

---

## CPU Utilization

**Formula:** `100 * (1 - avg_idle_rate_per_cpu)` — strips `cpu` and `mode` labels.

```sql
-- Current snapshot — all nodes
SELECT * FROM victoriametrics.infra_node_cpu_utilization
WHERE time = 'now';

-- Nodes above 80% right now
SELECT instance, value FROM victoriametrics.infra_node_cpu_utilization
WHERE time = 'now'
  AND value = '> 80';

-- Trend — last 6 hours, 5-minute resolution
SELECT instance, timestamp, value FROM victoriametrics.infra_node_cpu_utilization
WHERE time_start = 'now-6h'
  AND time_end = 'now'
  AND step = '5m';

-- Single server — last hour
SELECT * FROM victoriametrics.infra_node_cpu_utilization
WHERE instance = 'server01:9100'
  AND time_start = 'now-1h'
  AND step = '1m';
```

---

## Memory Utilization

**Formula:** `100 * (1 - MemAvailable / MemTotal)` — excludes buffers and cache (matches `free -h`).

```sql
-- Current snapshot — all nodes
SELECT * FROM victoriametrics.infra_node_memory_utilization
WHERE time = 'now';

-- Nodes above 90%
SELECT instance, value FROM victoriametrics.infra_node_memory_utilization
WHERE time = 'now'
  AND value = '> 90';

-- Trend — last 24 hours
SELECT instance, timestamp, value FROM victoriametrics.infra_node_memory_utilization
WHERE time_start = 'now-24h'
  AND time_end = 'now'
  AND step = '15m';
```

---

## Swap Utilization

**Formula:** `100 * (1 - SwapFree / SwapTotal)` — `clamp_min(SwapTotal, 1)` prevents divide-by-zero on nodes with no swap.

```sql
-- Any node currently using swap
SELECT instance, value FROM victoriametrics.infra_node_swap_utilization
WHERE time = 'now'
  AND value = '> 0';

-- Trend — last 12 hours
SELECT instance, timestamp, value FROM victoriametrics.infra_node_swap_utilization
WHERE time_start = 'now-12h'
  AND step = '5m';
```

---

## Disk Utilization

**Formula:** `100 * (1 - avail / size)` — excludes tmpfs, shm, cgroupfs, and `/repos` bind mounts.
**Extra label:** `mountpoint`

```sql
-- All mountpoints above 70% right now
SELECT instance, mountpoint, value FROM victoriametrics.infra_node_disk_utilization
WHERE time = 'now'
  AND value = '> 70';

-- Specific mountpoint — last 48 hours
SELECT instance, timestamp, value FROM victoriametrics.infra_node_disk_utilization
WHERE mountpoint = '/'
  AND time_start = 'now-48h'
  AND step = '30m';

-- Specific server — all mountpoints now
SELECT mountpoint, value FROM victoriametrics.infra_node_disk_utilization
WHERE instance = 'server01:9100'
  AND time = 'now';
```

---

## I/O Wait

**Formula:** `100 * avg_without(cpu, mode)(rate(node_cpu_seconds_total{mode="iowait"}[5m]))` — average iowait % across all CPUs.

```sql
-- Nodes with iowait above 20% right now
SELECT instance, value FROM victoriametrics.infra_node_iowait_pct
WHERE time = 'now'
  AND value = '> 20';

-- Trend — last 2 hours at 1-minute resolution
SELECT instance, timestamp, value FROM victoriametrics.infra_node_iowait_pct
WHERE time_start = 'now-2h'
  AND step = '1m';
```

---

## Load per CPU

**Formula:** `node_load1 / count(node_cpu_seconds_total{mode="idle"})` — 1-minute load average normalised by logical CPU count.
**Interpretation:** > 1.0 indicates CPU saturation.

```sql
-- Saturated nodes right now (load > 1.0 per CPU)
SELECT instance, value FROM victoriametrics.infra_node_load_per_cpu
WHERE time = 'now'
  AND value = '> 1';

-- Trend — last 6 hours
SELECT instance, timestamp, value FROM victoriametrics.infra_node_load_per_cpu
WHERE time_start = 'now-6h'
  AND step = '5m';
```

---

## Network Receive Rate

**Formula:** `sum_without(device)(rate(node_network_receive_bytes_total[5m]))` — sums all physical NICs, excludes lo, veth, docker, br-, cni, flannel, cali.
**Unit:** bytes/second

```sql
-- Current RX rate — all nodes
SELECT instance, value FROM victoriametrics.infra_node_network_rx_bytes_rate
WHERE time = 'now';

-- Trend — last 24 hours, 15-minute resolution
SELECT instance, timestamp, value FROM victoriametrics.infra_node_network_rx_bytes_rate
WHERE time_start = 'now-24h'
  AND time_end = 'now'
  AND step = '15m';
```

---

## Network Transmit Rate

**Formula:** same as RX but for `node_network_transmit_bytes_total`.
**Unit:** bytes/second

```sql
-- Current TX rate — all nodes
SELECT instance, value FROM victoriametrics.infra_node_network_tx_bytes_rate
WHERE time = 'now';

-- Trend — last 24 hours, 15-minute resolution
SELECT instance, timestamp, value FROM victoriametrics.infra_node_network_tx_bytes_rate
WHERE time_start = 'now-24h'
  AND time_end = 'now'
  AND step = '15m';
```

---

## Anomaly Detection Metrics

The anomaly detection pipeline produces two sets of metrics, both available as named tables:

| Named Table | PromQL Metric | Purpose |
|-------------|--------------|---------|
| `anomaly_zscore` | `anomaly:zscore` | Z-score per series — **primary alert signal** |
| `anomaly_level` | `anomaly:level` | Filtered input value with anomaly labels attached |
| `anomaly_upper_band` | `anomaly:upper_band` | Adaptive upper band (mean + 2σ) — Grafana only |
| `anomaly_lower_band` | `anomaly:lower_band` | Adaptive lower band (mean − 2σ) — Grafana only |

Warm-up: z-score requires ~1h (avg) and ~26h (stddev) to fully stabilise.

### `anomaly_name` Label Values

The `anomaly_name` label matches the derived metric name exactly:

| `anomaly_name` | Source Metric | `anomaly_strategy` |
|---|---|---|
| `cpu_utilization` | `infra:node:cpu_utilization` | `adaptive` |
| `memory_utilization` | `infra:node:memory_utilization` | `adaptive` |
| `swap_utilization` | `infra:node:swap_utilization` | `robust` |
| `disk_utilization` | `infra:node:disk_utilization` | `robust` |
| `iowait_pct` | `infra:node:iowait_pct` | `adaptive` |
| `load_per_cpu` | `infra:node:load_per_cpu` | `adaptive` |
| `network_rx_bytes_rate` | `infra:node:network_rx_bytes_rate` | `adaptive` |
| `network_tx_bytes_rate` | `infra:node:network_tx_bytes_rate` | `adaptive` |

---

## Z-Score Anomaly Detection

Z-score = (value − 1h_mean) / 26h_smoothed_stddev.
|z| ≥ 3 → critical anomaly; 2 ≤ |z| < 3 → major anomaly.

```sql
-- All current anomalies (|z| ≥ 2 across all metrics)
SELECT instance, meta_hostname, anomaly_name, value
  FROM victoriametrics.anomaly_zscore
  WHERE value = '> 2' AND time = 'now';

-- Critical spikes only (z ≥ 3)
SELECT instance, meta_hostname, anomaly_name, value
  FROM victoriametrics.anomaly_zscore
  WHERE value = '>= 3' AND time = 'now';

-- Critical drops only (z ≤ -3)
SELECT instance, meta_hostname, anomaly_name, value
  FROM victoriametrics.anomaly_zscore
  WHERE value = '<= -3' AND time = 'now';

-- Z-score for a specific metric — all servers now
SELECT instance, meta_hostname, value
  FROM victoriametrics.anomaly_zscore
  WHERE anomaly_name = 'cpu_utilization' AND time = 'now';

-- Z-score trend for one server — last 2 hours
SELECT timestamp, value
  FROM victoriametrics.anomaly_zscore
  WHERE anomaly_name = 'cpu_utilization'
    AND meta_hostname = 'dc1-dev-web-01'
    AND time_start = 'now-2h'
    AND step = '1m';

-- All anomaly metrics for one server — current z-scores
SELECT anomaly_name, value
  FROM victoriametrics.anomaly_zscore
  WHERE meta_hostname = 'dc1-dev-web-01' AND time = 'now';
```

---

## Anomaly Bands (Grafana Visualisation)

The adaptive bands show the expected range for each metric. Used in dashboards,
not for alerting. Require ~26 hours of data to fully stabilise.

```sql
-- CPU upper band — all servers now
SELECT instance, meta_hostname, value
  FROM victoriametrics.anomaly_upper_band
  WHERE anomaly_name = 'cpu_utilization' AND time = 'now';

-- Memory — actual value + both bands for one server
SELECT timestamp, value FROM victoriametrics.anomaly_level
  WHERE anomaly_name = 'memory_utilization'
    AND meta_hostname = 'dc1-dev-web-01'
    AND time_start = 'now-6h' AND step = '5m';

SELECT timestamp, value FROM victoriametrics.anomaly_upper_band
  WHERE anomaly_name = 'memory_utilization'
    AND meta_hostname = 'dc1-dev-web-01'
    AND time_start = 'now-6h' AND step = '5m';

SELECT timestamp, value FROM victoriametrics.anomaly_lower_band
  WHERE anomaly_name = 'memory_utilization'
    AND meta_hostname = 'dc1-dev-web-01'
    AND time_start = 'now-6h' AND step = '5m';

-- All upper bands for one server — current snapshot
SELECT anomaly_name, value FROM victoriametrics.anomaly_upper_band
  WHERE meta_hostname = 'dc1-dev-web-01' AND time = 'now';
```

---

## Platform & Container Recording Rules

Access via the generic `instant` / `range_query` tables with `WHERE metric = '...'`.

| Metric | Description | Labels |
|--------|-------------|--------|
| `oscar:host:cpu_usage_percent` | Host CPU usage % | `instance` |
| `oscar:host:cpu_iowait_percent` | Host iowait % | `instance` |
| `oscar:host:memory_usage_percent` | Host memory usage % | `instance` |
| `oscar:host:memory_available_bytes` | Available memory (bytes) | `instance` |
| `oscar:host:swap_usage_percent` | Swap usage % | `instance` |
| `oscar:host:disk_usage_percent` | Disk usage % per mountpoint | `instance`, `mountpoint` |
| `oscar:host:inode_usage_percent` | Inode usage % per mountpoint | `instance`, `mountpoint` |
| `oscar:host:reachable` | ICMP reachability (1=up, 0=down) | `instance` |
| `oscar:host:packet_loss_percent` | Packet loss % | `instance` |
| `oscar:container:cpu_usage_percent` | Container CPU usage % | `instance`, `name` |
| `oscar:container:memory_usage_percent` | Container memory % of limit | `instance`, `name` |
| `oscar:container:cpu_throttle_rate` | Container CPU throttle rate | `instance`, `name` |
| `oscar:platform:health` | OSCAR platform health (1=healthy) | `instance` |
| `oscar:platform:process_up` | Critical process status | `instance`, `process_pattern` |
| `oscar:vmdb:ingestion_rate` | VictoriaMetrics ingestion (rows/s) | `instance` |
| `oscar:vmdb:disk_usage_percent` | VictoriaMetrics disk usage % | `instance` |

```sql
-- Unreachable hosts
SELECT * FROM victoriametrics.instant
WHERE metric = 'oscar:host:reachable'
  AND value = '== 0';

-- Critical processes down
SELECT * FROM victoriametrics.instant
WHERE metric = 'oscar:platform:process_up'
  AND value = '== 0';

-- Platform health
SELECT * FROM victoriametrics.instant
WHERE metric = 'oscar:platform:health';

-- Containers using more than 10% CPU
SELECT * FROM victoriametrics.instant
WHERE metric = 'oscar:container:cpu_usage_percent'
  AND value = '> 10';

-- VMDB ingestion rate trend
SELECT * FROM victoriametrics.range_query
WHERE metric = 'oscar:vmdb:ingestion_rate'
  AND time_start = 'now-1h'
  AND step = '1m';
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Empty result with `value = '> N'` filter | All nodes are below the threshold — correct behaviour |
| `Binder Error: Referenced column "job" not found` | Use `SELECT *` or check labels first: `SELECT * FROM victoriametrics.labels WHERE metric = '...'` |
| Empty anomaly results | Check vmagent has node-exporter targets; bands need ~24h of data to stabilise |
| Wrong `anomaly_name` | Use exact metric names: `cpu_utilization`, `memory_utilization`, `swap_utilization`, `disk_utilization`, `iowait_pct`, `load_per_cpu`, `network_rx_bytes_rate`, `network_tx_bytes_rate` |
| `Cannot reach http://vmdb:8428` | Run `docker ps \| grep vmdb` to check VictoriaMetrics is up |
