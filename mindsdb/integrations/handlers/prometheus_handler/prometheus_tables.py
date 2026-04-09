import pandas as pd
from typing import List, Dict, Tuple, Optional

from mindsdb.integrations.libs.api_handler import APITable
from mindsdb.utilities import log
from mindsdb_sql_parser import ast
from mindsdb_sql_parser.ast import Star, Identifier, BinaryOperation, Constant

logger = log.getLogger(__name__)

# WHERE columns that are routing params, not PromQL label selectors
_ROUTING_COLS = {"metric", "fn", "fn_labels", "fn_window", "value", "time", "time_start", "time_end", "step"}

# Functions that require a range vector [window]
_RANGE_FNS = {"rate", "increase", "irate", "avg_over_time", "max_over_time", "min_over_time", "sum_over_time", "count_over_time"}

# Pure aggregation functions (no range vector needed)
_AGG_FNS = {"sum", "avg", "max", "min", "count", "stddev", "stdvar", "topk", "bottomk"}


def _build_promql(
    metric: str,
    labels: Dict[str, Tuple[str, str]],
    fn: Optional[str],
    fn_labels: Optional[str],
    fn_window: Optional[str],
    value_filter: Optional[str],
) -> str:
    """
    Builds a PromQL expression from parsed SQL WHERE components.

    Rules:
      metric='up', job='alertmanager'
        → up{job="alertmanager"}

      + fn='rate', fn_window='5m'
        → rate(up{job="alertmanager"}[5m])

      + fn='sum', fn_labels='job'
        → sum by(job) (up{...})

      + fn='sum:rate', fn_labels='job', fn_window='5m'
        → sum by(job) (rate(up{...}[5m]))

      + value='< 0.2'
        → <expression> < 0.2
    """
    if not metric:
        raise ValueError("WHERE metric = '<name>' is required")

    # Step 1 — build the base selector: metric{label="val", ...}
    selector_parts = []
    for col, (op, val) in labels.items():
        selector_parts.append(f'{col}{op}"{val}"')

    if selector_parts:
        inner = f"{metric}{{{', '.join(selector_parts)}}}"
    else:
        inner = metric

    # Step 2 — parse compound fn like 'sum:rate' or 'avg:rate'
    outer_fn = None
    inner_fn = None
    if fn:
        parts = fn.split(":", 1)
        if len(parts) == 2:
            outer_fn, inner_fn = parts[0], parts[1]
        elif fn in _RANGE_FNS:
            inner_fn = fn
        elif fn in _AGG_FNS:
            outer_fn = fn

    # Step 3 — wrap with range function: rate(...[5m])
    if inner_fn:
        w = fn_window or "5m"
        inner = f"{inner_fn}({inner}[{w}])"

    # Step 4 — wrap with aggregation: sum by(job) (...)
    if outer_fn:
        if fn_labels:
            by_clause = ", ".join(l.strip() for l in fn_labels.split(","))
            inner = f"{outer_fn} by({by_clause}) ({inner})"
        else:
            inner = f"{outer_fn}({inner})"

    # Step 5 — append threshold filter: expression < 0.2
    if value_filter:
        inner = f"{inner} {value_filter}"

    return inner


def _walk_where(node) -> List[Tuple[str, str, str]]:
    """
    Recursively walk a WHERE AST node and return list of (op, col, val) tuples.

    Supports all operators that extract_comparison_conditions handled
    (=, !=, >, <, >=, <=) PLUS LIKE, NOT LIKE, and OR.

    OR on the same column is combined into a single condition so that
      meta_hostname LIKE '%web%' OR meta_hostname LIKE '%db%'
    becomes  ('like', 'meta_hostname', '%web%|%db%')
    which the caller converts to PromQL regex  meta_hostname=~".*web.*|.*db.*"
    """
    if node is None:
        return []

    if not isinstance(node, BinaryOperation):
        return []

    op = node.op.lower()

    # AND: flatten both sides
    if op == 'and':
        return _walk_where(node.args[0]) + _walk_where(node.args[1])

    # OR: combine same-column LIKE patterns; fall back to separate conditions
    if op == 'or':
        left = _walk_where(node.args[0])
        right = _walk_where(node.args[1])
        combined = list(left)
        for r_op, r_col, r_val in right:
            merged = False
            for i, (l_op, l_col, l_val) in enumerate(combined):
                if l_col == r_col and l_op == r_op and l_op in ('like', 'not like'):
                    # Combine patterns: '%web%|%db%'
                    combined[i] = (l_op, l_col, f"{l_val}|{r_val}")
                    merged = True
                    break
            if not merged:
                combined.append((r_op, r_col, r_val))
        return combined

    # Leaf comparison: =, !=, >, <, >=, <=, like, not like
    _SUPPORTED = {'=', '!=', '>', '<', '>=', '<=', 'like', 'not like', 'in', 'not in'}
    if op in _SUPPORTED and len(node.args) == 2:
        left, right = node.args
        if isinstance(left, Identifier) and isinstance(right, Constant):
            col = left.parts[-1].lower()
            val = str(right.value) if right.value is not None else ""
            return [(op, col, val)]
        if isinstance(right, Identifier) and isinstance(left, Constant):
            col = right.parts[-1].lower()
            val = str(left.value) if left.value is not None else ""
            return [(op, col, val)]

    return []


def _parse_conditions(query: ast.Select) -> dict:
    """
    Walks query.where and separates routing columns (metric, fn, start, ...)
    from PromQL label selectors.

    Supports =, !=, >, <, >=, <=, LIKE, NOT LIKE, and OR on label columns.
    """
    result = {
        "metric": None,
        "fn": None,
        "fn_labels": None,
        "fn_window": None,
        "value_filter": None,
        "time": None,
        "time_start": "now-1h",
        "time_end": "now",
        "step": "1m",
        "labels": {},      # {col: (op_str, val)}
    }

    if query.where is None:
        return result

    conditions = _walk_where(query.where)

    # operator mapping from SQL to PromQL
    op_map = {
        "=":    "=",
        "!=":   "!=",
        "like": "=~",
        "not like": "!~",
    }

    for op, col, val in conditions:
        col = col.lower()
        val = str(val) if val is not None else ""

        if col == "metric":
            result["metric"] = val
        elif col == "fn":
            result["fn"] = val
        elif col == "fn_labels":
            result["fn_labels"] = val
        elif col in ("fn_window", "window"):
            result["fn_window"] = val
        elif col == "value":
            # value = '< 0.2'  or  value = '== 0'  or  value = '> 80'
            # val already contains the operator+number as a string
            result["value_filter"] = val
        elif col == "time":
            result["time"] = val
        elif col in ("time_start", "start"):
            result["time_start"] = val
        elif col in ("time_end", "end"):
            result["time_end"] = val
        elif col == "step":
            result["step"] = val
        else:
            # Everything else is a PromQL label selector
            promql_op = op_map.get(op, "=")
            # Convert SQL LIKE wildcards to PromQL regex:
            #   %web%  →  .*web.*
            #   web%   →  web.*
            #   _      →  . (single-char wildcard)
            if op in ("like", "not like"):
                val = val.replace(".", r"\.").replace("%", ".*").replace("_", ".")
            result["labels"][col] = (promql_op, val)

    return result


def _ensure_columns(df: pd.DataFrame, columns: List[str]) -> pd.DataFrame:
    """
    Ensures all listed columns exist in df.
    Missing columns are added as pd.NA (DuckDB-compatible null).
    This is critical when VictoriaMetrics returns empty results — without this,
    MindsDB's internal DuckDB engine raises a BinderError for any column
    that is declared in get_columns() but absent from the empty DataFrame.
    """
    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA
    return df


def _filter_columns(df: pd.DataFrame, query: ast.Select) -> pd.DataFrame:
    """
    Filters DataFrame columns based on SELECT targets.
    SELECT *           → return all columns
    SELECT a, b, c    → return only those columns (add pd.NA for missing ones)
    Matches Twitter handler convention.
    """
    columns = []
    for target in query.targets:
        if isinstance(target, Star):
            return df  # SELECT * — return everything
        elif isinstance(target, Identifier):
            columns.append(target.parts[-1].lower())

    if not columns:
        return df

    # add missing columns as pd.NA so result shape is always predictable
    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA

    return df[columns]


def _victoriametrics_result_to_df(raw: list, result_type: str) -> pd.DataFrame:
    """
    Converts VictoriaMetrics JSON result list to a flat DataFrame.

    Instant query result item:
      {"metric": {"__name__": "up", "job": "alertmanager", ...}, "value": [timestamp, "1"]}

    Range query result item:
      {"metric": {"__name__": "up", "job": "alertmanager", ...}, "values": [[ts, "1"], [ts, "1"], ...]}
    """
    rows = []

    if result_type == "vector":
        for item in raw:
            row = dict(item.get("metric", {}))
            ts, val = item["value"]
            row["timestamp"] = ts
            row["value"] = val
            rows.append(row)

    elif result_type == "matrix":
        for item in raw:
            labels = dict(item.get("metric", {}))
            for ts, val in item.get("values", []):
                row = dict(labels)
                row["timestamp"] = ts
                row["value"] = val
                rows.append(row)

    if not rows:
        return pd.DataFrame(columns=["metric", "value", "timestamp"])

    df = pd.DataFrame(rows)

    # Rename __name__ to metric for cleaner SQL results
    if "__name__" in df.columns:
        df.rename(columns={"__name__": "metric"}, inplace=True)

    # Cast value to numeric where possible
    df["value"] = pd.to_numeric(df["value"], errors="coerce")

    return df


class PrometheusInstantTable(APITable):
    """
    Handles:  SELECT ... FROM victoriametrics.instant WHERE ...
    Endpoint: GET /api/v1/query
    Returns:  one row per time series (current value snapshot)
    """

    def select(self, query: ast.Select) -> pd.DataFrame:
        params = _parse_conditions(query)
        promql = _build_promql(
            metric=params["metric"],
            labels=params["labels"],
            fn=params["fn"],
            fn_labels=params["fn_labels"],
            fn_window=params["fn_window"],
            value_filter=params["value_filter"],
        )

        logger.debug(f"[prometheus_handler] instant query → PromQL: {promql}")

        api_params = {"query": promql}
        if params["time"]:
            api_params["time"] = params["time"]

        raw = self.handler.call_prometheus_api("/api/v1/query", api_params, table_name="instant")

        result_type = raw.get("data", {}).get("resultType", "vector")
        result = raw.get("data", {}).get("result", [])

        df = _victoriametrics_result_to_df(result, result_type)
        df = _ensure_columns(df, self.get_columns())

        if query.limit is not None:
            df = df.head(int(query.limit.value))

        return _filter_columns(df, query)

    def get_columns(self) -> List[str]:
        return ["metric", "value", "timestamp", "job", "instance", "datacenter", "environment"]


class PrometheusRangeTable(APITable):
    """
    Handles:  SELECT ... FROM victoriametrics.range_query WHERE ...
    Endpoint: GET /api/v1/query_range
    Returns:  one row per (time series, timestamp) pair

    Time range — two equivalent syntaxes:
      WHERE time_start = 'now-1h' AND time_end = 'now'   -- preferred (no quoting needed)
      WHERE `start` = 'now-1h' AND `end` = 'now'         -- backtick-quoted reserved words

    Defaults if omitted: time_start='now-1h', time_end='now', step='1m'
    """

    def select(self, query: ast.Select) -> pd.DataFrame:
        params = _parse_conditions(query)
        promql = _build_promql(
            metric=params["metric"],
            labels=params["labels"],
            fn=params["fn"],
            fn_labels=params["fn_labels"],
            fn_window=params["fn_window"],
            value_filter=params["value_filter"],
        )

        logger.debug(f"[prometheus_handler] range query → PromQL: {promql}")

        api_params = {
            "query": promql,
            "start": params["time_start"],
            "end": params["time_end"] or "now",
            "step": params["step"],
        }

        raw = self.handler.call_prometheus_api("/api/v1/query_range", api_params, table_name="range_query")

        result_type = raw.get("data", {}).get("resultType", "matrix")
        result = raw.get("data", {}).get("result", [])

        df = _victoriametrics_result_to_df(result, result_type)
        df = _ensure_columns(df, self.get_columns())

        if query.limit is not None:
            df = df.head(int(query.limit.value))

        return _filter_columns(df, query)

    def get_columns(self) -> List[str]:
        return ["metric", "value", "timestamp", "job", "instance", "datacenter", "environment"]


class PrometheusMetricsTable(APITable):
    """
    Handles:  SELECT ... FROM victoriametrics.metrics WHERE ...
    Endpoint: GET /api/v1/label/__name__/values
    Returns:  all metric names available in VictoriaMetrics (for discovery)
    """

    def select(self, query: ast.Select) -> pd.DataFrame:
        raw = self.handler.call_prometheus_api("/api/v1/label/__name__/values", {}, table_name="metrics")
        names = raw.get("data", [])
        df = pd.DataFrame({"metric_name": names})

        if query.limit is not None:
            df = df.head(int(query.limit.value))

        return df

    def get_columns(self) -> List[str]:
        return ["metric_name"]


class PrometheusLabelsTable(APITable):
    """
    Handles:  SELECT ... FROM victoriametrics.labels WHERE metric = 'up'
    Endpoint: GET /api/v1/series
    Returns:  all label combinations for a metric (for discovery)
    """

    def select(self, query: ast.Select) -> pd.DataFrame:
        params = _parse_conditions(query)

        match = params["metric"] or "{}"
        raw = self.handler.call_prometheus_api(
            "/api/v1/series", {"match[]": match}, table_name="labels"
        )

        series = raw.get("data", [])
        if not series:
            return pd.DataFrame(columns=self.get_columns())

        rows = []
        for s in series:
            rows.append(dict(s))

        df = pd.DataFrame(rows)
        if "__name__" in df.columns:
            df.rename(columns={"__name__": "metric"}, inplace=True)

        if query.limit is not None:
            df = df.head(int(query.limit.value))

        return df

    def get_columns(self) -> List[str]:
        return ["metric", "job", "instance", "datacenter", "environment"]


class _NamedMetricTable(APITable):
    """
    Base class for pre-configured tables that wrap a single PromQL metric.

    Behaviour:
      - Default:         range query (last 1h, 1m step)
      - WHERE time=      instant query at that timestamp
      - WHERE time_start / time_end / step:  custom range query

    The metric name is baked in — users do not need WHERE metric = '...'.
    All other WHERE conditions are passed through as PromQL label selectors.
    Subclasses override get_columns() to declare their specific label set.
    """

    def __init__(self, handler, metric_name: str):
        super().__init__(handler)
        self.metric_name = metric_name

    def select(self, query: ast.Select) -> pd.DataFrame:
        params = _parse_conditions(query)

        # Metric name is fixed for this table — ignore any WHERE metric = ...
        params["metric"] = self.metric_name

        promql = _build_promql(
            metric=params["metric"],
            labels=params["labels"],
            fn=params["fn"],
            fn_labels=params["fn_labels"],
            fn_window=params["fn_window"],
            value_filter=params["value_filter"],
        )

        logger.debug(
            f"[prometheus_handler] named_metric table={self.metric_name} → PromQL: {promql}"
        )

        if params["time"]:
            # Instant query — single value per series
            raw = self.handler.call_prometheus_api(
                "/api/v1/query",
                {"query": promql, "time": params["time"]},
                table_name=self.metric_name,
            )
            result_type = raw.get("data", {}).get("resultType", "vector")
        else:
            # Range query — full time-series (user explicitly set step or using defaults)
            raw = self.handler.call_prometheus_api(
                "/api/v1/query_range",
                {
                    "query": promql,
                    "start": params["time_start"],
                    "end": params["time_end"] or "now",
                    "step": params["step"],
                },
                table_name=self.metric_name,
            )
            result_type = raw.get("data", {}).get("resultType", "matrix")

        result = raw.get("data", {}).get("result", [])
        df = _victoriametrics_result_to_df(result, result_type)
        df = _ensure_columns(df, self.get_columns())

        if query.limit is not None:
            df = df.head(int(query.limit.value))

        return _filter_columns(df, query)

    def get_columns(self) -> List[str]:
        raise NotImplementedError


class InfraRecordingRuleTable(_NamedMetricTable):
    """
    Pre-configured table for an infrastructure node recording rule metric.

    Each instance wraps one infra:node:* derived metric and exposes it as a
    named table in the victoriametrics database (visible in SHOW TABLES).

    The mountpoint column is populated for infra_node_disk_utilization only;
    it is returned as NA for all other metrics.

    Examples:
      SELECT * FROM victoriametrics.infra_node_cpu_utilization
        WHERE meta_hostname = 'dc1-dev-web-01' AND time = 'now';

      SELECT value, timestamp, meta_hostname
        FROM victoriametrics.infra_node_memory_utilization
        WHERE meta_hostname LIKE '%prod%'
          AND time_start = 'now-6h'
          AND step = '5m';

      SELECT mountpoint, value FROM victoriametrics.infra_node_disk_utilization
        WHERE meta_hostname = 'dc1-dev-app-01'
          AND mountpoint = '/'
          AND time = 'now';
    """

    def get_columns(self) -> List[str]:
        return [
            "metric", "value", "timestamp",
            "instance", "job",
            "meta_hostname", "meta_ipaddress",
            "datacenter", "environment",
            "oscar_metric_type",
            "mountpoint",  # populated for disk_utilization, NA for all others
        ]


class InfraAnomalyTable(_NamedMetricTable):
    """
    Pre-configured table for an anomaly detection metric.

    Each instance wraps one anomaly:* metric (z-score, adaptive bands, level)
    and exposes it as a named table in the victoriametrics database.

    Filter by anomaly_name to select a specific node metric:
      cpu_utilization, memory_utilization, swap_utilization, disk_utilization,
      iowait_pct, load_per_cpu, network_rx_bytes_rate, network_tx_bytes_rate

    The anomaly_method column is populated for anomaly_zscore only (value: 'zscore').

    Examples:
      -- Current z-scores above 3σ (critical anomalies)
      SELECT instance, anomaly_name, value
        FROM victoriametrics.anomaly_zscore
        WHERE value = '> 3' AND time = 'now';

      -- CPU z-score trend for one server
      SELECT timestamp, value FROM victoriametrics.anomaly_zscore
        WHERE anomaly_name = 'cpu_utilization'
          AND meta_hostname = 'dc1-dev-web-01'
          AND time_start = 'now-2h'
          AND step = '1m';

      -- Adaptive upper band for memory (Grafana context, not alerting)
      SELECT instance, anomaly_name, value
        FROM victoriametrics.anomaly_upper_band
        WHERE anomaly_name = 'memory_utilization' AND time = 'now';
    """

    def get_columns(self) -> List[str]:
        return [
            "metric", "value", "timestamp",
            "instance", "job",
            "meta_hostname", "meta_ipaddress",
            "datacenter", "environment",
            "anomaly_name", "anomaly_strategy", "anomaly_type",
            "anomaly_method",  # populated for anomaly_zscore only
        ]


# ---------------------------------------------------------------------------
# Registry: maps MindsDB table name → PromQL metric name
# Infrastructure node derived metrics (recording-oscar-node-derived.on.yml)
# ---------------------------------------------------------------------------
INFRA_RECORDING_RULE_TABLES: Dict[str, str] = {
    "infra_node_cpu_utilization":         "infra:node:cpu_utilization",
    "infra_node_memory_utilization":      "infra:node:memory_utilization",
    "infra_node_swap_utilization":        "infra:node:swap_utilization",
    "infra_node_disk_utilization":        "infra:node:disk_utilization",
    "infra_node_iowait_pct":              "infra:node:iowait_pct",
    "infra_node_load_per_cpu":            "infra:node:load_per_cpu",
    "infra_node_network_rx_bytes_rate":   "infra:node:network_rx_bytes_rate",
    "infra_node_network_tx_bytes_rate":   "infra:node:network_tx_bytes_rate",
}

# ---------------------------------------------------------------------------
# Registry: maps MindsDB table name → PromQL metric name
# Anomaly detection metrics (recording-anomaly-zscore.on.yml +
#                            recording-anomaly-adaptive.on.yml)
# ---------------------------------------------------------------------------
INFRA_ANOMALY_TABLES: Dict[str, str] = {
    # Z-score anomaly detection — primary alerting metric
    "anomaly_zscore":       "anomaly:zscore",
    # Adaptive pipeline — filtered input value (same as the source metric, with anomaly labels)
    "anomaly_level":        "anomaly:level",
    # Adaptive bands — Grafana visualization only, not used for alerting
    "anomaly_upper_band":   "anomaly:upper_band",
    "anomaly_lower_band":   "anomaly:lower_band",
}


# ===========================================================================
# OSCAR Platform Operational Tables
# ===========================================================================
# These tables expose real-time OSCAR service metrics scraped from the
# oscar-monitor, oscar-alertmanager, oscar-taskmanager, and oscar-vector-gateway
# Prometheus endpoints.  All source metrics are Gauges — they return meaningful
# point-in-time values without requiring rate() or increase() transforms.
# ===========================================================================

class OscarContainerTable(_NamedMetricTable):
    """
    Active notification dispatch tasks from alertmanager_middleware.

    Metric: oscar_alertmanager_active_notification_tasks
    Value:  current count of in-flight notification tasks

    Labels: (none — scalar Gauge)

    Examples:
      -- How many notification tasks are active right now?
      SELECT value
        FROM victoriametrics.oscar_alert_active_tasks
        WHERE time = 'now';
    """

    def get_columns(self) -> List[str]:
        return [
            "metric", "value", "timestamp",
        ]


class OscarAlertQueueTable(_NamedMetricTable):
    """
    Alert queue depth or processing rate from oscar-alertmanager.

    Metric: oscar_alert_queue_depth
    Labels: queue  (queue name: tm_alerts, tm_notifier, etc.)

    Examples:
      -- Current depth of all alert queues
      SELECT queue, value
        FROM victoriametrics.oscar_alert_queue_depth
        WHERE time = 'now';

      -- Is there a queue backlog?
      SELECT queue, value
        FROM victoriametrics.oscar_alert_queue_depth
        WHERE value = '> 0' AND time = 'now';
    """

    def get_columns(self) -> List[str]:
        return [
            "metric", "value", "timestamp",
            "job", "instance",
            "queue",
        ]


class OscarAlertCircuitBreakerTable(_NamedMetricTable):
    """
    Circuit breaker open/closed state from Celery task monitor or AI backends.

    Metrics:
      celery_monitor_circuit_breaker_open  — taskmanager Celery circuit breaker (0=closed, 1=open)
      oscar_topic_classifier_backend_health — topic classifier backend health (1=healthy, 0=unhealthy)

    Labels: job, instance, datacenter, environment

    Examples:
      -- Is the taskmanager circuit breaker open?
      SELECT value, datacenter, environment
        FROM victoriametrics.oscar_alert_circuit_breaker
        WHERE time = 'now';

      -- Is the topic classifier backend healthy?
      SELECT value, datacenter, environment
        FROM victoriametrics.oscar_topic_classifier_health
        WHERE time = 'now';
    """

    def get_columns(self) -> List[str]:
        return [
            "metric", "value", "timestamp",
            "job", "instance",
            "datacenter", "environment",
        ]


class OscarAlertCounterTable(_NamedMetricTable):
    """
    Alert processing counters from alertmanager_middleware.

    Metric: oscar_alertmanager_alerts_processed_total
    Value:  cumulative count of alerts processed
    Labels: status  (success, error)

    Examples:
      -- How many alerts have been processed total?
      SELECT status, value
        FROM victoriametrics.oscar_alert_processed
        WHERE time = 'now';
    """

    def get_columns(self) -> List[str]:
        return [
            "metric", "value", "timestamp",
            "status",
        ]


class OscarTaskTable(_NamedMetricTable):
    """
    Task execution metrics from oscar-taskmanager (Celery monitoring).

    Used for:
      oscar_task_history  → oscar_task_success              (Gauge pushed via pushgateway, per-task result)
      oscar_task_rate     → celery_monitor_task_execution_rate  (Gauge, scalar tasks/sec)
      oscar_task_workers  → celery_monitor_active_workers       (Gauge, scalar count)

    Labels for oscar_task_history (oscar_task_success):
      task_name, task_type, state (SUCCESS/FAILURE), meta_hostname, meta_component, meta_datacenter
    No labels for oscar_task_rate and oscar_task_workers.

    Examples:
      -- Which tasks are failing?
      SELECT task_name, task_type, state, meta_hostname, value
        FROM victoriametrics.oscar_task_history
        WHERE state = 'FAILURE' AND time = 'now';

      -- All task execution results grouped by task name
      SELECT task_name, state, value
        FROM victoriametrics.oscar_task_history
        WHERE time = 'now';

      -- Current task execution rate (tasks/sec)
      SELECT value FROM victoriametrics.oscar_task_rate
        WHERE time = 'now';

      -- Active worker count
      SELECT value FROM victoriametrics.oscar_task_workers
        WHERE time = 'now';
    """

    def get_columns(self) -> List[str]:
        return [
            "metric", "value", "timestamp",
            "task_name", "task_type", "state",
            "meta_hostname", "meta_component", "meta_datacenter",
            "job", "instance",
        ]


class OscarVectorTable(_NamedMetricTable):
    """
    Notifier delivery failure metrics from oscar-notifier.

    Metric: oscar_notifier_failed_total
    Value:  cumulative count of failed notification deliveries
    Labels: notifier_name, notifier_type, namespace, error_type, provider

    Examples:
      -- Which notifiers are failing?
      SELECT notifier_name, notifier_type, error_type, provider, value
        FROM victoriametrics.oscar_notifier_failed
        WHERE time = 'now';

      -- Failures by error type
      SELECT error_type, value
        FROM victoriametrics.oscar_notifier_failed
        WHERE time = 'now';
    """

    def get_columns(self) -> List[str]:
        return [
            "metric", "value", "timestamp",
            "notifier_name", "notifier_type",
            "namespace", "error_type", "provider",
            "job", "instance",
        ]


# ---------------------------------------------------------------------------
# Registry: maps MindsDB table name → PromQL metric name
# OSCAR platform operational metrics — all Gauges, no recording rules needed
# ---------------------------------------------------------------------------

OSCAR_PLATFORM_TABLES: Dict[str, str] = {
    # alertmanager_middleware: active in-flight notification dispatch tasks
    "oscar_alert_active_tasks":     "oscar_alertmanager_active_notification_tasks",
}

OSCAR_ALERT_TABLES: Dict[str, str] = {
    # alertmanager_middleware: live queue depth per Celery queue
    "oscar_alert_queue_depth":      "oscar_alert_queue_depth",
    # alertmanager_middleware: cumulative alerts processed by status (success/error)
    "oscar_alert_processed":        "oscar_alertmanager_alerts_processed_total",
    # taskmanager celery monitor: circuit breaker open/closed (0=closed, 1=open)
    "oscar_alert_circuit_breaker":  "celery_monitor_circuit_breaker_open",
}

OSCAR_TASK_TABLES: Dict[str, str] = {
    # taskmanager pushgateway: per-task execution result (state=SUCCESS/FAILURE, 80+ series)
    "oscar_task_history":   "oscar_task_success",
    # taskmanager: task execution rate (tasks/sec over last 5 min)
    "oscar_task_rate":      "celery_monitor_task_execution_rate",
    # taskmanager: number of active Celery workers
    "oscar_task_workers":   "celery_monitor_active_workers",
}

OSCAR_SERVICE_TABLES: Dict[str, str] = {
    # oscar-notifier: cumulative failed notification deliveries with error details
    "oscar_notifier_failed":         "oscar_notifier_failed_total",
    # middleware: topic classifier AI backend health (1=healthy, 0=unhealthy)
    "oscar_topic_classifier_health": "oscar_topic_classifier_backend_health",
}
