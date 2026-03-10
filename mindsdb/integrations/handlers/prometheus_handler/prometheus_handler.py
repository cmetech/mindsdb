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
    OscarRecordingRuleTable,
    OSCAR_RECORDING_RULE_TABLES,
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

    OSCAR recording rule tables (each maps to one pre-computed metric):
      oscar_node_cpu_utilization       - CPU usage %
      oscar_node_memory_utilization    - Memory usage % (excl. buffers/cache)
      oscar_node_swap_utilization      - Swap usage %
      oscar_node_disk_utilization      - Disk usage % per mountpoint
      oscar_node_iowait_pct            - I/O wait %
      oscar_node_load_per_cpu          - Load average / vCPU count
      oscar_node_network_rx_bytes_rate - Network receive bytes/s
      oscar_node_network_tx_bytes_rate - Network transmit bytes/s
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

        # OSCAR recording rule tables — one table per derived metric.
        # Each table name maps to the PromQL recording rule metric defined in
        # oscar-metricstore/vmalert/rules/recording-oscar-node-derived.on.yml
        for table_name, metric_name in OSCAR_RECORDING_RULE_TABLES.items():
            self._register_table(table_name, OscarRecordingRuleTable(self, metric_name))

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
