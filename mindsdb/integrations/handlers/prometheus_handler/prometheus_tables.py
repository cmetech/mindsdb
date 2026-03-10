import pandas as pd
from typing import List, Dict, Tuple, Optional

from mindsdb.integrations.libs.api_handler import APITable
from mindsdb.integrations.utilities.sql_utils import extract_comparison_conditions
from mindsdb.utilities import log
from mindsdb_sql_parser import ast
from mindsdb_sql_parser.ast import Star, Identifier

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


def _parse_conditions(query: ast.Select) -> dict:
    """
    Walks query.where via extract_comparison_conditions and separates
    routing columns (metric, fn, start, ...) from PromQL label selectors.
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

    conditions = extract_comparison_conditions(query.where)

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


class OscarRecordingRuleTable(APITable):
    """
    A pre-configured table for a specific OSCAR recording rule metric.

    Each instance wraps one oscar:node:* metric and exposes it as a named
    table in the victoriametrics database (visible in SHOW TABLES).

    Behaviour:
      - Default:         range query (last 1h, 1m step)
      - WHERE time=      instant query at that timestamp
      - WHERE time_start / time_end / step:  custom range query

    All other WHERE conditions are passed through as PromQL label selectors.
    The metric name is baked in — users do not need WHERE metric = '...'.

    Examples:
      SELECT * FROM victoriametrics.oscar_node_cpu_utilization
        WHERE instance = 'server01:9100';

      SELECT value, timestamp, instance
        FROM victoriametrics.oscar_node_memory_utilization
        WHERE instance LIKE 'prod.*' AND time_start = 'now-6h' AND step = '5m';
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
            f"[prometheus_handler] recording_rule table={self.metric_name} → PromQL: {promql}"
        )

        if params["time"]:
            # Instant query
            raw = self.handler.call_prometheus_api(
                "/api/v1/query",
                {"query": promql, "time": params["time"]},
                table_name=self.metric_name,
            )
            result_type = raw.get("data", {}).get("resultType", "vector")
        else:
            # Range query (default)
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
        return [
            "metric", "value", "timestamp",
            "instance", "job", "datacenter", "environment",
            "oscar_metric_type",
        ]


# ---------------------------------------------------------------------------
# Registry: maps MindsDB table name → PromQL metric name
# Add new OSCAR recording rules here as they are created.
# ---------------------------------------------------------------------------
OSCAR_RECORDING_RULE_TABLES: Dict[str, str] = {
    "oscar_node_cpu_utilization":      "oscar:node:cpu_utilization",
    "oscar_node_memory_utilization":   "oscar:node:memory_utilization",
    "oscar_node_swap_utilization":     "oscar:node:swap_utilization",
    "oscar_node_disk_utilization":     "oscar:node:disk_utilization",
    "oscar_node_iowait_pct":           "oscar:node:iowait_pct",
    "oscar_node_load_per_cpu":         "oscar:node:load_per_cpu",
    "oscar_node_network_rx_bytes_rate": "oscar:node:network_rx_bytes_rate",
    "oscar_node_network_tx_bytes_rate": "oscar:node:network_tx_bytes_rate",
}
