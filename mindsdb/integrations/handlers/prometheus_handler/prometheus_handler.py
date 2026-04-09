import requests
from typing import Optional

from mindsdb.integrations.libs.api_handler import APIHandler, FuncParser
from mindsdb.integrations.libs.response import HandlerStatusResponse as StatusResponse, HandlerResponse as Response, RESPONSE_TYPE
from mindsdb.utilities import log

from .prometheus_tables import (
    PrometheusInstantTable,
    PrometheusRangeTable,
    PrometheusMetricsTable,
    PrometheusLabelsTable,
    InfraRecordingRuleTable,
    InfraAnomalyTable,
    OscarContainerTable,
    OscarAlertQueueTable,
    OscarAlertCircuitBreakerTable,
    OscarAlertCounterTable,
    OscarTaskTable,
    OscarVectorTable,
    INFRA_RECORDING_RULE_TABLES,
    INFRA_ANOMALY_TABLES,
    OSCAR_PLATFORM_TABLES,
    OSCAR_ALERT_TABLES,
    OSCAR_TASK_TABLES,
    OSCAR_SERVICE_TABLES,
    _victoriametrics_result_to_df,
)

logger = log.getLogger(__name__)


class PrometheusHandler(APIHandler):
    """
    MindsDB handler for Prometheus-compatible HTTP APIs.
    Tested against VictoriaMetrics but compatible with Thanos, Mimir, Cortex.

    CREATE DATABASE victoriametrics
    WITH ENGINE = 'prometheus',
    PARAMETERS = {
        "host": "http://vmdb:8428",
        "timeout": 30
    };

    Built-in tables:
      instant      - Instant (snapshot) queries via /api/v1/query
      range_query  - Range queries via /api/v1/query_range
      metrics      - Metric name discovery via /api/v1/label/__name__/values
      labels       - Label discovery via /api/v1/series

    Infrastructure node tables (each maps to one pre-computed derived metric):
      infra_node_cpu_utilization         - CPU usage %
      infra_node_memory_utilization      - Memory usage % (excl. buffers/cache)
      infra_node_swap_utilization        - Swap usage %
      infra_node_disk_utilization        - Disk usage % per mountpoint (+ mountpoint col)
      infra_node_iowait_pct              - I/O wait %
      infra_node_load_per_cpu            - Load average / vCPU count
      infra_node_network_rx_bytes_rate   - Network receive bytes/s
      infra_node_network_tx_bytes_rate   - Network transmit bytes/s

    Anomaly detection tables (z-score alerting + adaptive band visualisation):
      anomaly_zscore     - Z-score per metric/instance (primary alert signal)
      anomaly_level      - Filtered input value with anomaly labels
      anomaly_upper_band - Adaptive upper band (mean + 2σ, Grafana only)
      anomaly_lower_band - Adaptive lower band (mean − 2σ, Grafana only)

    OSCAR platform operational tables (Gauges/Counters scraped from live services):
      oscar_alert_active_tasks    - Active in-flight notification dispatch tasks (scalar)
      oscar_alert_queue_depth     - Live alert queue depth per Celery queue
      oscar_alert_processed       - Cumulative alerts processed by status (success/error)
      oscar_alert_circuit_breaker - Celery taskmanager circuit breaker state (0=closed, 1=open)
      oscar_task_history          - Per-task execution results via pushgateway (80+ series)
      oscar_task_rate             - Task execution rate (tasks/sec, 5-min avg)
      oscar_task_workers          - Active Celery worker count
      oscar_notifier_failed       - Failed notification deliveries by notifier/error_type
      oscar_topic_classifier_health - Topic classifier AI backend health (1=healthy, 0=unhealthy)
    """

    name = "prometheus"

    def __init__(self, name=None, **kwargs):
        super().__init__(name)

        self.connection_data = kwargs.get('connection_data', {})
        self.base_url = self.connection_data.get("host", "").rstrip("/")
        self.timeout = int(self.connection_data.get("timeout", 30))
        self.verify_ssl = self.connection_data.get("verify_ssl", False)

        username = self.connection_data.get("username")
        password = self.connection_data.get("password")
        self.auth = (username, password) if username and password else None

        self.is_connected = False
        self._session: Optional[requests.Session] = None

        # Core query tables
        self._register_table("instant",      PrometheusInstantTable(self))
        self._register_table("range_query",  PrometheusRangeTable(self))
        self._register_table("metrics",      PrometheusMetricsTable(self))
        self._register_table("labels",       PrometheusLabelsTable(self))

        # Infrastructure node tables — one table per derived metric.
        # Each table name maps to the PromQL recording rule metric defined in
        # oscar-metricstore/vmalert/rules/recording-oscar-node-derived.on.yml
        for table_name, metric_name in INFRA_RECORDING_RULE_TABLES.items():
            self._register_table(table_name, InfraRecordingRuleTable(self, metric_name))

        # Anomaly detection tables — z-score + adaptive band metrics.
        # Defined in recording-anomaly-zscore.on.yml + recording-anomaly-adaptive.on.yml
        for table_name, metric_name in INFRA_ANOMALY_TABLES.items():
            self._register_table(table_name, InfraAnomalyTable(self, metric_name))

        # OSCAR platform operational tables — Gauges/Counters from live OSCAR services.
        for table_name, metric_name in OSCAR_PLATFORM_TABLES.items():
            self._register_table(table_name, OscarContainerTable(self, metric_name))

        for table_name, metric_name in OSCAR_ALERT_TABLES.items():
            if metric_name == "oscar_alertmanager_alerts_processed_total":
                self._register_table(table_name, OscarAlertCounterTable(self, metric_name))
            elif metric_name == "celery_monitor_circuit_breaker_open":
                self._register_table(table_name, OscarAlertCircuitBreakerTable(self, metric_name))
            else:
                self._register_table(table_name, OscarAlertQueueTable(self, metric_name))

        for table_name, metric_name in OSCAR_TASK_TABLES.items():
            self._register_table(table_name, OscarTaskTable(self, metric_name))

        for table_name, metric_name in OSCAR_SERVICE_TABLES.items():
            if metric_name == "oscar_topic_classifier_backend_health":
                self._register_table(table_name, OscarAlertCircuitBreakerTable(self, metric_name))
            else:
                self._register_table(table_name, OscarVectorTable(self, metric_name))

    def connect(self) -> requests.Session:
        if self.is_connected and self._session:
            return self._session

        session = requests.Session()
        if self.auth:
            session.auth = self.auth

        self._session = session
        self.is_connected = True
        logger.info(f"[prometheus_handler] connected to {self.base_url}")
        return session

    def disconnect(self):
        if self._session:
            self._session.close()
            self._session = None
        self.is_connected = False

    def check_connection(self) -> StatusResponse:
        """
        Validates connectivity by hitting /api/v1/query?query=1
        which is the lightest possible valid PromQL query.
        """
        try:
            session = self.connect()
            resp = session.get(
                f"{self.base_url}/api/v1/query",
                params={"query": "1"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            resp.raise_for_status()
            logger.info(f"[prometheus_handler] connection check OK ({resp.status_code})")
            return StatusResponse(success=True)

        except requests.exceptions.ConnectionError as e:
            msg = f"Cannot reach {self.base_url}: {e}"
            logger.error(f"[prometheus_handler] {msg}")
            self.is_connected = False
            return StatusResponse(success=False, error_message=msg)

        except requests.exceptions.HTTPError as e:
            msg = f"HTTP error from {self.base_url}: {e}"
            logger.error(f"[prometheus_handler] {msg}")
            self.is_connected = False
            return StatusResponse(success=False, error_message=msg)

        except Exception as e:
            msg = f"Unexpected error checking connection: {e}"
            logger.error(f"[prometheus_handler] {msg}")
            self.is_connected = False
            return StatusResponse(success=False, error_message=msg)

    def native_query(self, query_string: str = None):
        """Execute a raw PromQL query string directly.

        Example: SELECT * FROM victoriametrics (up{job="alertmanager"})
        """
        raw = self.call_prometheus_api('/api/v1/query', {'query': query_string})
        result = raw.get('data', {}).get('result', [])
        result_type = raw.get('data', {}).get('resultType', 'vector')
        df = _victoriametrics_result_to_df(result, result_type)
        return Response(RESPONSE_TYPE.TABLE, data_frame=df)

    def call_prometheus_api(self, path: str, params: dict, table_name: str = "") -> dict:
        """
        Makes a GET request to the Prometheus HTTP API.

        Parameters
        ----------
        path : str
            API path, e.g. '/api/v1/query' or '/api/v1/query_range'
        params : dict
            Query parameters, e.g. {'query': 'up', 'start': '...', 'end': '...', 'step': '1m'}

        Returns
        -------
        dict
            Parsed JSON response body. Raises on non-success API status.
        """
        session = self.connect()
        url = f"{self.base_url}{path}"

        logger.debug(f"[prometheus_handler] GET {url} params={params}")

        try:
            resp = session.get(
                url,
                params=params,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"[prometheus_handler] API request failed: {e}")
            raise

        body = resp.json()

        if body.get("status") != "success":
            err = body.get("error", "Unknown error from Prometheus API")
            logger.error(f"[prometheus_handler] API returned status={body.get('status')}: {err}")
            raise RuntimeError(f"Prometheus API error: {err}")

        return body
