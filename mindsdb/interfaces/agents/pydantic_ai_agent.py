"""Pydantic AI Agent wrapper to replace LangchainAgent"""

import json
import re
import warnings
import functools
from typing import Dict, List, Optional, Any, Iterable

from openai import OpenAI as _SyncOpenAI

import pandas as pd
from mindsdb_sql_parser import parse_sql, ast
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, ModelMessage, TextPart

from mindsdb.utilities import log
from mindsdb.interfaces.storage import db
from mindsdb.interfaces.agents.utils.constants import (
    USER_COLUMN,
    ASSISTANT_COLUMN,
    CONTEXT_COLUMN,
    TRACE_ID_COLUMN,
)

from mindsdb.interfaces.agents.utils.sql_toolkit import MindsDBQuery
from mindsdb.interfaces.agents.utils.pydantic_ai_model_factory import get_model_instance_from_kwargs
from mindsdb.interfaces.agents.utils.data_catalog_builder import DataCatalogBuilder, dataframe_to_markdown
from mindsdb.utilities.context import context as ctx
from mindsdb.utilities.langfuse import LangfuseClientWrapper
from mindsdb.interfaces.agents.modes import sql as sql_mode, text_sql as text_sql_mode
from mindsdb.interfaces.agents.modes.base import ResponseType, PlanResponse

logger = log.getLogger(__name__)
DEBUG_LOGGER = logger.debug


def _model_name_to_variant(model_name: str) -> str:
    """Normalise a model name to a prompt manager variant name.

    Variant names must be URL-safe identifiers. The rule is: replace ':' and '/'
    with '-', then lowercase. This gives a deterministic, human-readable mapping
    that admins can reproduce without consulting the code:

        qwen2.5:3b   → qwen2.5-3b
        llama3.2:3b  → llama3.2-3b
        gpt-4o-mini  → gpt-4o-mini   (unchanged — already safe)
        ollama/llama3.2 → ollama-llama3.2

    The same name is shown in the prompt manager UI's Model dropdown, which
    lists available models from the LLM gateway (same list as Chat Playground).
    """
    return re.sub(r'[^a-z0-9._-]', '-', model_name.lower()).strip('-')


def _fetch_prompt_from_manager(slug: str, environment: str, fallback: str, model_name: str = "") -> str:
    """Fetch system prompt from oscar-promptmanager via the /fetch endpoint.

    Resolution order (first success wins):
      1. variant_ref: "{slug}/{model_variant}"  — model-specific prompt if a variant
         named after the model exists in the prompt manager.  Variant name is derived
         from model_name via _model_name_to_variant() (e.g. qwen2.5:3b → qwen2.5-3b).
         Skipped if model_name is empty or variant returns 404.
      2. environment_ref: "{slug}/{environment}" — the revision currently deployed to
         the named environment (default: 'production').  This is the universal fallback
         for models that don't have their own variant.
      3. agent.params['prompt_template']          — hardcoded prompt stored in MindsDB DB.
         Used when PROMPTMANAGER_ENABLED=false or promptmanager is unreachable.

    Falls back to `fallback` if:
      - PROMPTMANAGER_ENABLED is not 'true'
      - promptmanager is unreachable (timeout / connection error)
      - slug not found or not deployed (404 on both variant and environment)
      - response has no system message content

    Args:
        slug:        Prompt slug (e.g. 'oscar-ops-agent')
        environment: Deployment environment fallback (e.g. 'production')
        fallback:    prompt_template stored in agent params — last resort
        model_name:  LLM model name (e.g. 'qwen2.5:3b') — used to derive variant name
    """
    import os
    import urllib.request
    import urllib.error

    pm_enabled = os.getenv("PROMPTMANAGER_ENABLED", "false").lower() in ("true", "1", "t")
    if not pm_enabled:
        return fallback

    pm_host = os.getenv("PROMPTMANAGER_HOST", "promptmanager")
    pm_port = os.getenv("PROMPTMANAGER_PORT", "2300")
    url = f"http://{pm_host}:{pm_port}/api/v1/fetch"

    def _extract_system_message(data: dict) -> str:
        """Pull the system role message content from a fetch response."""
        for msg in data.get("config", {}).get("messages", []):
            if msg.get("role") == "system" and msg.get("content", "").strip():
                return msg["content"].strip()
        return ""

    def _do_fetch(body: dict, label: str) -> str:
        """POST to /fetch, return system message content or empty string."""
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Internal-Service": "kore"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                content = _extract_system_message(data)
                if content:
                    logger.info(
                        "[KORE] Loaded system prompt from promptmanager %s revision=%s",
                        label, data.get("revision_number", "?")
                    )
                else:
                    logger.warning("[KORE] Promptmanager %s returned no system message", label)
                return content
        except urllib.error.HTTPError as e:
            if e.code != 404:
                logger.warning("[KORE] Promptmanager %s → HTTP %s", label, e.code)
            return ""
        except Exception as e:
            logger.warning("[KORE] Promptmanager %s → %s", label, e)
            return ""

    # Step 1 — model-specific variant (e.g. variant_ref = "oscar-ops-agent/qwen2.5-3b")
    if model_name:
        variant = _model_name_to_variant(model_name)
        content = _do_fetch({"variant_ref": f"{slug}/{variant}"}, f"variant_ref={slug}/{variant}")
        if content:
            return content

    # Step 2 — production environment (e.g. environment_ref = "oscar-ops-agent/production")
    content = _do_fetch({"environment_ref": f"{slug}/{environment}"}, f"environment_ref={slug}/{environment}")
    if content:
        return content

    # Step 3 — hardcoded fallback in agent params
    logger.warning("[KORE] All promptmanager lookups failed for slug='%s' — using hardcoded fallback", slug)
    return fallback

# JSON schema for AgentResponse — used by the response_format fallback below
_AGENT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "sql_query":        {"type": "string", "description": "SQL query to execute, or empty string for final_text"},
        "text":             {"type": "string", "description": "Human-readable answer for the user"},
        "short_description":{"type": "string", "description": "One-line summary of what this step does"},
        "type":             {"type": "string", "enum": ["final_query", "exploratory_query", "final_text"],
                             "description": "final_query=run SQL and return result, exploratory_query=run SQL to gather info, final_text=answer without SQL"},
    },
    "required": ["sql_query", "text", "short_description", "type"],
}


_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Concise natural language answer to the user's question"},
    },
    "required": ["answer"],
}

_PLANNING_SCHEMA = {
    "type": "object",
    "properties": {
        "plan":            {"type": "string",  "description": "Brief description of the query plan"},
        "estimated_steps": {"type": "integer", "description": "Number of SQL queries needed to answer the question"},
    },
    "required": ["plan", "estimated_steps"],
    "additionalProperties": False,
}


def _is_direct_json_provider(llm_params: dict) -> bool:
    """Return True for providers where pydantic-ai tool-calling is unreliable.

    These providers work correctly with _direct_llm_json_call (response_format=json_schema)
    but fail when pydantic-ai uses tool-based structured output — producing assistant messages
    with content=null that Ollama rejects as 'invalid message content type: <nil>'.
    Each failed attempt burns one full inference round-trip (~15-35s) before the exception
    is raised, so we skip straight to _direct_llm_json_call for these providers.
    """
    provider = llm_params.get("provider", "")
    if provider == "ollama":
        return True
    # Also detect ollama-compatible endpoints by URL (e.g. socat bridge, LM Studio on 11434)
    base_url = (
        llm_params.get("ollama_base_url")
        or llm_params.get("openai_api_base")
        or llm_params.get("base_url")
        or ""
    )
    return "11434" in base_url or "ollama" in base_url.lower()


def _direct_llm_json_call(llm_params: dict, system_prompt: str, user_prompt: str, schema: dict = None) -> dict:
    """Call an OpenAI-compatible API with response_format=json_schema.

    Used as a fallback when pydantic-ai tool-calling fails (e.g. with small models
    like llama3.2 that don't reliably follow the final_result tool convention).
    """
    base_url = (
        llm_params.get("openai_api_base")
        or llm_params.get("ollama_base_url")
        or "http://localhost:11434/v1"
    )
    api_key = llm_params.get("api_key", "ollama")
    model_name = llm_params.get("model_name", "llama3.2")
    active_schema = schema if schema is not None else _AGENT_RESPONSE_SCHEMA

    client = _SyncOpenAI(base_url=base_url, api_key=api_key)
    resp = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "AgentResponse", "schema": active_schema},
        },
        max_tokens=1000,
    )
    return json.loads(resp.choices[0].message.content)


_HEALTH_SUMMARY_SENTINEL = "__HEALTH_SUMMARY__"

# Alert severity display — icon and sort order.
# Update here if severities change; do not hardcode in formatter functions.
_SEVERITY_ICONS = {"critical": "🔴", "major": "🟠", "minor": "🟡", "warning": "⚪", "info": "🔵"}
_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "warning": 3, "info": 4, "unknown": 5}


def _is_health_summary(question: str) -> bool:
    """Return True if the question asks for a multi-domain health overview."""
    q = question.lower()
    multi_domain = sum([
        any(k in q for k in ["alert", "firing", "incident", "fire", "triggered"]),
        any(k in q for k in ["task", "failed", "automation", "job", "probe"]),
        any(k in q for k in ["cpu", "memory", "infrastructure", "infra", "server", "resource"]),
    ])
    return multi_domain >= 2 or any(k in q for k in [
        "health summary", "full summary", "overview", "health check", "overall",
        "how is", "how are", "system status", "platform status", "everything ok",
        "anything wrong", "all good", "status report",
    ])


def _build_oscar_data_catalog() -> str:
    """Return a rich static data catalog for all OSCAR data sources.

    Replaces the auto-generated catalog from DataCatalogBuilder which produces
    empty rows for VictoriaMetrics tables (they require WHERE time = 'now').

    This catalog mirrors MindsDB Enterprise quality:
    each table entry has PURPOSE, MANDATORY FILTER, COLUMNS with semantics,
    VALUE CONSTRAINTS, and EXAMPLE SQL — enough for any model to generate
    correct SQL on first attempt without needing sample rows.
    """
    return """=== OSCAR DATA CATALOG ===

ROUTING RULE: Use victoriametrics.* for live metrics/performance. Use oscar_db.* for alerts, tasks, notifications.
Both data sources must always be queried separately — never JOIN across sources.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DATA SOURCE: victoriametrics
All victoriametrics tables REQUIRE: WHERE time = 'now'  (point-in-time snapshot — omitting this returns nothing)
Value filtering MUST be done in Python, not SQL WHERE — never write WHERE value > N
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

--- Table: victoriametrics.infra_node_cpu_utilization ---
PURPOSE: CPU utilization per server/node. Use for: high CPU, busy servers, processor load, compute usage.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS:
  meta_hostname  — server/node hostname (e.g. "web-01.prod")
  value          — CPU percentage 0-100 (higher = busier)
  datacenter     — datacenter name
  environment    — environment tag (prod/staging/dev)
EXAMPLE: SELECT meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization WHERE time = 'now' ORDER BY value DESC LIMIT 10

--- Table: victoriametrics.infra_node_memory_utilization ---
PURPOSE: Memory (RAM) utilization per server. Excludes OS buffers/cache. Use for: memory pressure, OOM risk, RAM usage.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS:
  meta_hostname  — server hostname
  value          — memory percentage 0-100 (higher = more used)
  datacenter, environment
EXAMPLE: SELECT meta_hostname, value FROM victoriametrics.infra_node_memory_utilization WHERE time = 'now' ORDER BY value DESC LIMIT 10

--- Table: victoriametrics.infra_node_swap_utilization ---
PURPOSE: Swap space utilization. High swap indicates memory shortage/paging activity.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: meta_hostname, value (swap % 0-100), datacenter, environment
EXAMPLE: SELECT meta_hostname, value FROM victoriametrics.infra_node_swap_utilization WHERE time = 'now' ORDER BY value DESC

--- Table: victoriametrics.infra_node_disk_utilization ---
PURPOSE: Disk space utilization per mount point. Use for: disk full, running out of space, storage capacity.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS:
  meta_hostname  — server hostname
  value          — disk percentage 0-100 (higher = less space)
  mountpoint     — disk mount point (e.g. "/", "/data", "/var")
  datacenter, environment
EXAMPLE: SELECT meta_hostname, mountpoint, value FROM victoriametrics.infra_node_disk_utilization WHERE time = 'now' ORDER BY value DESC LIMIT 10

--- Table: victoriametrics.infra_node_iowait_pct ---
PURPOSE: I/O wait percentage per server. High values indicate storage bottleneck or slow disk.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: meta_hostname, value (I/O wait % 0-100), datacenter, environment
EXAMPLE: SELECT meta_hostname, value FROM victoriametrics.infra_node_iowait_pct WHERE time = 'now' ORDER BY value DESC

--- Table: victoriametrics.infra_node_load_per_cpu ---
PURPOSE: Load average divided by CPU count (normalized). Values > 1.0 mean system is overloaded.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: meta_hostname, value (load/vCPU, >1.0 = overloaded), datacenter, environment
EXAMPLE: SELECT meta_hostname, value FROM victoriametrics.infra_node_load_per_cpu WHERE time = 'now' ORDER BY value DESC

--- Table: victoriametrics.infra_node_network_rx_bytes_rate ---
PURPOSE: Network receive rate in bytes/second per server. Use for: network throughput, bandwidth, ingress traffic.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: meta_hostname, value (bytes/sec received), datacenter, environment
EXAMPLE: SELECT meta_hostname, value FROM victoriametrics.infra_node_network_rx_bytes_rate WHERE time = 'now' ORDER BY value DESC

--- Table: victoriametrics.infra_node_network_tx_bytes_rate ---
PURPOSE: Network transmit rate in bytes/second per server. Use for: outgoing bandwidth, egress traffic.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: meta_hostname, value (bytes/sec transmitted), datacenter, environment
EXAMPLE: SELECT meta_hostname, value FROM victoriametrics.infra_node_network_tx_bytes_rate WHERE time = 'now' ORDER BY value DESC

--- Table: victoriametrics.anomaly_zscore ---
PURPOSE: Z-score anomaly detection per metric per server. Use for: unusual behavior, anomalies, outliers, anything behaving strangely.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS:
  meta_hostname  — server hostname
  anomaly_name   — name of the metric being monitored (cpu, memory, network, etc.)
  value          — z-score (>2.0 = anomalous, >3.0 = highly anomalous, <2.0 = normal)
  datacenter
IMPORTANT: Do NOT filter WHERE value > N — return all rows; the formatter identifies anomalies from values.
EXAMPLE: SELECT meta_hostname, anomaly_name, value FROM victoriametrics.anomaly_zscore WHERE time = 'now' ORDER BY value DESC LIMIT 20

--- Table: victoriametrics.anomaly_level ---
PURPOSE: Raw input metric value with anomaly labels. Use to see the actual metric value alongside anomaly context.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: meta_hostname, anomaly_name, value (actual metric value), datacenter
EXAMPLE: SELECT meta_hostname, anomaly_name, value FROM victoriametrics.anomaly_level WHERE time = 'now'

--- Table: victoriametrics.oscar_alert_active_tasks ---
PURPOSE: Count of in-flight notification dispatch tasks right now. Use for: is the alertmanager busy, backlog.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: metric, value (count of active tasks), timestamp
EXAMPLE: SELECT value FROM victoriametrics.oscar_alert_active_tasks WHERE time = 'now'

--- Table: victoriametrics.oscar_alert_queue_depth ---
PURPOSE: Live alert queue depth per Celery queue. Use for: queue backlog, processing lag, queue health.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: metric, queue (queue name: tm_alerts/tm_notifier/etc.), value (depth), timestamp, job, instance
EXAMPLE: SELECT queue, value FROM victoriametrics.oscar_alert_queue_depth WHERE time = 'now' ORDER BY value DESC

--- Table: victoriametrics.oscar_alert_processed ---
PURPOSE: Cumulative count of alerts processed by status. Use for: how many alerts processed, success/error rates.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: metric, status (success/error), value (cumulative count), timestamp
EXAMPLE: SELECT status, value FROM victoriametrics.oscar_alert_processed WHERE time = 'now'

--- Table: victoriametrics.oscar_alert_circuit_breaker ---
PURPOSE: Celery taskmanager circuit breaker state. 0=closed (healthy), 1=open (tripped/failing).
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: metric, value (0=closed/OK, 1=open/failing), datacenter, environment, timestamp
EXAMPLE: SELECT value, datacenter FROM victoriametrics.oscar_alert_circuit_breaker WHERE time = 'now'

--- Table: victoriametrics.oscar_task_history ---
PURPOSE: Per-task execution results (SUCCESS/FAILURE) pushed via pushgateway. Use for: which tasks are failing, task health.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: metric, task_name, task_type, state (SUCCESS/FAILURE), meta_hostname, meta_component, meta_datacenter, value, timestamp
EXAMPLE: SELECT task_name, task_type, state, meta_hostname FROM victoriametrics.oscar_task_history WHERE time = 'now'

--- Table: victoriametrics.oscar_task_rate ---
PURPOSE: Task execution rate (tasks/second, 5-minute average). Use for: throughput, tasks per second.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: metric, value (tasks/sec), timestamp
EXAMPLE: SELECT value FROM victoriametrics.oscar_task_rate WHERE time = 'now'

--- Table: victoriametrics.oscar_task_workers ---
PURPOSE: Number of active Celery workers right now. Use for: worker count, capacity.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: metric, value (worker count), timestamp
EXAMPLE: SELECT value FROM victoriametrics.oscar_task_workers WHERE time = 'now'

--- Table: victoriametrics.oscar_notifier_failed ---
PURPOSE: Failed notification deliveries with error details. Use for: notification failures, delivery errors.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: metric, notifier_name, notifier_type, namespace, error_type, provider, value (failure count), timestamp
EXAMPLE: SELECT notifier_name, notifier_type, error_type, provider, value FROM victoriametrics.oscar_notifier_failed WHERE time = 'now'

--- Table: victoriametrics.oscar_topic_classifier_health ---
PURPOSE: AI topic classifier backend health. 1=healthy, 0=unhealthy.
MANDATORY FILTER: WHERE time = 'now'
COLUMNS: metric, value (1=healthy/0=unhealthy), datacenter, environment, timestamp
EXAMPLE: SELECT value, datacenter FROM victoriametrics.oscar_topic_classifier_health WHERE time = 'now'

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DATA SOURCE: oscar_db
MySQL operational database. No mandatory time filter — use standard SQL.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

--- Table: oscar_db.AM_AlertHistory ---
PURPOSE: All alerts ever fired in OSCAR. Use for: firing alerts, alert history, incidents, acknowledged alerts.
COLUMNS:
  alertname       — alert rule name (e.g. "HighCPU", "DiskFull")
  status          — "firing" (active now) or "resolved" (cleared)
  severity        — "critical", "major", "minor", "warning", "info"
  summary         — human-readable description of the alert
  startsAt        — when alert first fired (datetime)
  endsAt          — when alert resolved (datetime, NULL if still firing)
  acknowledged    — 0 (not ack'd) or 1 (acknowledged by operator)
  occurrence_count — how many times this alert fired
  last_occurrence — timestamp of most recent firing
VALUE CONSTRAINTS: status IN ('firing','resolved'); severity IN ('critical','major','minor','warning','info')
EXAMPLE: SELECT alertname, severity, summary, last_occurrence FROM oscar_db.AM_AlertHistory WHERE status = 'firing' AND last_occurrence > DATE_SUB(NOW(), INTERVAL 24 HOUR) ORDER BY last_occurrence DESC

--- Table: oscar_db.TM_History ---
PURPOSE: Task execution history log. Always JOIN with oscar_db.TM_Tasks on task_id. Use for: task failures, running tasks, execution results.
COLUMNS (alias h):
  h.id      — history record id
  h.task_id — foreign key to TM_Tasks.id
  h.state   — "SUCCESS", "FAILURE", "FAILED", "PENDING", "IN_PROGRESS"
  h.started — when task started (datetime)
  h.succeeded — when task succeeded (datetime, NULL if not succeeded)
  h.runtime — execution duration (seconds)
  h.result  — result message / error text
JOIN: oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
VALUE CONSTRAINTS: h.state IN ('SUCCESS','FAILURE','FAILED','PENDING','IN_PROGRESS')
EXAMPLE: SELECT t.name, h.state, h.result, h.started FROM oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id WHERE h.state IN ('FAILURE','FAILED') AND h.started > DATE_SUB(NOW(), INTERVAL 1 HOUR) ORDER BY h.started DESC

--- Table: oscar_db.TM_Tasks ---
PURPOSE: Task definition catalog. Always used via JOIN with TM_History.
COLUMNS (alias t):
  t.id          — task id (primary key)
  t.name        — task name (human-readable label)
  t.type        — task type ("fabric", "ansible", "script", "probe")
  t.description — what the task does
JOIN: oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id

--- Table: oscar_db.NTF_Notifications_Audit ---
PURPOSE: Notification delivery audit log. Use for: failed notifications, email/webhook delivery issues, retry counts.
COLUMNS:
  notifier_name   — name of the notifier (e.g. "ops-email", "pagerduty-webhook")
  alert_name      — name of the alert that triggered notification
  status          — "sent", "failed", "suppressed"
  recipient       — destination address/URL
  retry_count     — number of delivery retries
  status_details  — error message or success info
VALUE CONSTRAINTS: status IN ('sent','failed','suppressed')
EXAMPLE: SELECT notifier_name, alert_name, status_details, retry_count FROM oscar_db.NTF_Notifications_Audit WHERE status = 'failed' ORDER BY id DESC
"""


def _parse_time_interval(q: str):
    """Extract time interval from natural language question.

    Returns (n, unit, description) where unit is a SQL INTERVAL unit string,
    or (None, None, None) if no time range is found.

    Examples:
      "last 2 hours"   → (2, "HOUR",   "last 2 hours")
      "last 30 mins"   → (30, "MINUTE", "last 30 minutes")
      "last 10 min"    → (10, "MINUTE", "last 10 minutes")
      "past 3 days"    → (3, "DAY",    "last 3 days")
      "last 1 hour"    → (1, "HOUR",   "last 1 hour")
    """
    import re
    m = re.search(
        r'(?:last|past)\s+(\d+)\s*(min(?:ute)?s?|h(?:our)?s?|d(?:ay)?s?)',
        q, re.IGNORECASE
    )
    if not m:
        return None, None, None
    n = int(m.group(1))
    raw = m.group(2).lower()
    if raw.startswith('h'):
        return n, "HOUR", f"last {n} hour{'s' if n != 1 else ''}"
    if raw.startswith('m'):
        return n, "MINUTE", f"last {n} minute{'s' if n != 1 else ''}"
    if raw.startswith('d'):
        return n, "DAY", f"last {n} day{'s' if n != 1 else ''}"
    return None, None, None


def _get_routing_hint(question: str) -> str:
    """Return a deterministic routing hint: exact table name, column names, and example SQL.

    This is a model-agnostic quality layer — not a small-model workaround. By handing the
    LLM the exact table and column names upfront, SQL generation becomes reliable across all
    model sizes (llama.cpp 1B, qwen2.5:3b, GPT-4o, Claude). The LLM only needs to write SQL;
    it does not need to recall schema details from training data.

    Returns _HEALTH_SUMMARY_SENTINEL for multi-domain summary questions.
    """
    if _is_health_summary(question):
        return _HEALTH_SUMMARY_SENTINEL
    q = question.lower()
    if any(k in q for k in [
        "cpu", "processor", "utilization", "load average", "cpu load",
        "how busy", "busy server", "consuming", "compute", "processing power",
    ]):
        return (
            "You must query the table named: victoriametrics.infra_node_cpu_utilization\n"
            "Exact column names: meta_hostname, value, datacenter, environment\n"
            "value = CPU percentage (0-100). Required filter: WHERE time = 'now'\n"
            "Use this SQL (adapt filters as needed):\n"
            "SELECT meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization WHERE time = 'now' ORDER BY value DESC LIMIT 5\n\n"
        )
    if any(k in q for k in [
        "memory", " mem ", "ram", "heap", "out of memory", "oom",
        "swap usage", "swapping", "memory pressure",
    ]):
        return (
            "You must query the table named: victoriametrics.infra_node_memory_utilization\n"
            "Exact column names: meta_hostname, value, datacenter, environment\n"
            "value = memory percentage (0-100). Required filter: WHERE time = 'now'\n"
            "Use this SQL (adapt filters as needed):\n"
            "SELECT meta_hostname, value FROM victoriametrics.infra_node_memory_utilization WHERE time = 'now' ORDER BY value DESC LIMIT 5\n\n"
        )
    if any(k in q for k in [
        "disk", "storage", "inode", "volume", "partition", "filesystem",
        "running out of space", "low on space", "disk full", "disk space",
        "space left", "free space",
    ]):
        return (
            "You must query the table named: victoriametrics.infra_node_disk_utilization\n"
            "Exact column names: meta_hostname, value, datacenter, environment\n"
            "value = disk percentage (0-100). Required filter: WHERE time = 'now'\n"
            "Use this SQL (adapt filters as needed):\n"
            "SELECT meta_hostname, value FROM victoriametrics.infra_node_disk_utilization WHERE time = 'now' ORDER BY value DESC LIMIT 5\n\n"
        )
    if any(k in q for k in [
        "anomal", "zscore", "z-score", "behav", "unusual", "abnormal",
        "misbehav", "weird", "strange", "out of pattern", "deviation", "outlier",
    ]):
        return (
            "You must query the table named: victoriametrics.anomaly_zscore\n"
            "Exact column names: meta_hostname, anomaly_name, value, datacenter\n"
            "value = z-score (value > 2.0 means anomalous). Do NOT filter on value in WHERE — return all rows and identify anomalies from the values.\n"
            "Use this SQL (do not add any AND value filter):\n"
            "SELECT meta_hostname, anomaly_name, value FROM victoriametrics.anomaly_zscore WHERE time = 'now' ORDER BY value DESC LIMIT 20\n\n"
        )
    if any(k in q for k in [
        "alert", "firing", "incident", "acknowledged",
        "fire", "triggered", "open issue", "active alarm", "alarm",
        "any fires", "what fired", "raised",
    ]):
        n, unit, desc = _parse_time_interval(q)
        # Determine whether the user wants currently-firing only, or all alerts in a time window.
        wants_firing = any(k in q for k in ["firing", "fire", "active", "open", "current", "now", "right now"])
        if n is not None:
            # User specified a time window (e.g. "last 2 hours", "last 30 mins")
            time_filter = f"last_occurrence > DATE_SUB(NOW(), INTERVAL {n} {unit})"
            if wants_firing:
                where_clause = f"WHERE status = 'firing' AND {time_filter}"
            else:
                where_clause = f"WHERE {time_filter}"
            _alert_example_sql = f"SELECT alertname, severity, summary, last_occurrence FROM oscar_db.AM_AlertHistory {where_clause} ORDER BY last_occurrence DESC"
            _alert_time_note = f"Time window for this question: {desc}"
        else:
            # No time range specified — default to currently firing
            _alert_example_sql = "SELECT alertname, severity, summary, last_occurrence FROM oscar_db.AM_AlertHistory WHERE status = 'firing' ORDER BY last_occurrence DESC"
            _alert_time_note = "No time window specified — return all currently firing alerts (status = 'firing')"
        return (
            "You must query the table named: oscar_db.AM_AlertHistory\n"
            "Exact column names: alertname, status, severity, summary, startsAt, endsAt, acknowledged, occurrence_count, last_occurrence\n"
            "Valid status values: firing, resolved\n"
            "Valid severity values: critical, major, minor, warning, info\n"
            f"{_alert_time_note}\n"
            "Use EXACTLY this SQL (do NOT modify the WHERE clause time filter — do NOT add LIMIT):\n"
            f"{_alert_example_sql}\n\n"
        )
    if any(k in q for k in [
        "task", "celery", "job run", "automation", "probe", "playbook",
        "script run", "ansible", "fabric", "scheduled job", "execution",
        "job fail", "probe fail", "automation fail", "failing probe",
    ]):
        # Determine which state filter to suggest based on the question intent.
        tn, tunit, tdesc = _parse_time_interval(q)
        _task_time_filter = f" AND h.started > DATE_SUB(NOW(), INTERVAL {tn} {tunit})" if tn else ""
        _task_time_note = f"Time window: {tdesc}" if tn else "No time window — return all matching records"
        if any(k in q for k in ["running", "in progress", "in_progress", "active", "current", "executing", "under way"]):
            _task_state_filter = "h.state = 'IN_PROGRESS'"
            _task_state_example = f"SELECT t.name, h.state, h.started FROM oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id WHERE h.state = 'IN_PROGRESS'{_task_time_filter} ORDER BY h.started DESC"
        elif any(k in q for k in ["pending", "waiting", "queue", "queued", "scheduled"]):
            _task_state_filter = "h.state = 'PENDING'"
            _task_state_example = f"SELECT t.name, h.state, h.started FROM oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id WHERE h.state = 'PENDING'{_task_time_filter} ORDER BY h.started DESC"
        elif any(k in q for k in ["success", "succeeded", "completed", "passed", "done", "finished"]):
            _task_state_filter = "h.state = 'SUCCESS'"
            _task_state_example = f"SELECT t.name, h.state, h.started, h.runtime FROM oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id WHERE h.state = 'SUCCESS'{_task_time_filter} ORDER BY h.started DESC"
        else:
            _task_state_filter = "h.state IN ('FAILURE','FAILED')"
            _task_time_filter = _task_time_filter if tn else " AND h.started > DATE_SUB(NOW(), INTERVAL 1 HOUR)"
            _task_state_example = f"SELECT t.name, h.state, h.result, h.started FROM oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id WHERE h.state IN ('FAILURE','FAILED'){_task_time_filter} ORDER BY h.started DESC"
        return (
            "You must query tables: oscar_db.TM_History and oscar_db.TM_Tasks\n"
            "IMPORTANT: always use table aliases and prefix every column — never use bare column names.\n"
            "TM_History exact columns: h.id, h.task_id, h.state, h.started, h.succeeded, h.runtime, h.result\n"
            "TM_Tasks exact columns: t.id, t.name, t.type, t.description\n"
            "JOIN condition: oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id\n"
            f"State filter for this question: {_task_state_filter}\n"
            f"{_task_time_note}\n"
            "Valid state values: SUCCESS, FAILURE, FAILED, PENDING, IN_PROGRESS\n"
            "Use EXACTLY this SQL (do NOT modify the WHERE clause time filter):\n"
            f"{_task_state_example}\n\n"
        )
    if any(k in q for k in [
        "notif", "notifier", "email", "webhook", "delivery",
        "notification", "alert delivery", "notify", "sent alert",
        "failed to send", "delivery fail",
    ]):
        return (
            "You must query the table named: oscar_db.NTF_Notifications_Audit\n"
            "Exact column names: notifier_name, alert_name, status, recipient, retry_count, status_details\n"
            "Valid status values: sent, failed, suppressed\n"
            "Use this SQL (adapt filters as needed — do NOT add LIMIT, return all results):\n"
            "SELECT notifier_name, alert_name, status_details, retry_count FROM oscar_db.NTF_Notifications_Audit WHERE status = 'failed' ORDER BY id DESC\n\n"
        )
    return ""


_EXAMPLE_SQL = {
    "cpu":           "SELECT meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization WHERE time = 'now' ORDER BY value DESC LIMIT 5",
    "memory":        "SELECT meta_hostname, value FROM victoriametrics.infra_node_memory_utilization WHERE time = 'now' ORDER BY value DESC LIMIT 5",
    "disk":          "SELECT meta_hostname, value FROM victoriametrics.infra_node_disk_utilization WHERE time = 'now' ORDER BY value DESC LIMIT 5",
    "anomaly":       "SELECT meta_hostname, anomaly_name, value FROM victoriametrics.anomaly_zscore WHERE time = 'now' ORDER BY value DESC LIMIT 20",
    "alert":         "SELECT alertname, severity, summary, last_occurrence FROM oscar_db.AM_AlertHistory WHERE status = 'firing' AND last_occurrence > DATE_SUB(NOW(), INTERVAL 24 HOUR) ORDER BY last_occurrence DESC",
    "task":          "SELECT t.name, h.state, h.result, h.started FROM oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id WHERE h.state IN ('FAILURE','FAILED') AND h.started > DATE_SUB(NOW(), INTERVAL 1 HOUR) ORDER BY h.started DESC",
    "task_running":  "SELECT t.name, h.state, h.started FROM oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id WHERE h.state = 'IN_PROGRESS' ORDER BY h.started DESC",
    "task_pending":  "SELECT t.name, h.state, h.started FROM oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id WHERE h.state = 'PENDING' ORDER BY h.started DESC",
    "notif":         "SELECT notifier_name, alert_name, status_details, retry_count FROM oscar_db.NTF_Notifications_Audit WHERE status = 'failed' ORDER BY id DESC",
    "health_alerts": "SELECT alertname, severity, status, summary FROM oscar_db.AM_AlertHistory WHERE status = 'firing' AND last_occurrence > DATE_SUB(NOW(), INTERVAL 24 HOUR) ORDER BY last_occurrence DESC",
    "health_tasks":  "SELECT t.name, h.state, h.result FROM oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id WHERE h.state IN ('FAILURE','FAILED') AND h.started > DATE_SUB(NOW(), INTERVAL 1 HOUR) ORDER BY h.started DESC",
    "health_cpu":    "SELECT meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization WHERE time = 'now' ORDER BY value DESC LIMIT 5",
}


def _apply_value_threshold(question: str, df: "pd.DataFrame") -> tuple:
    """Apply numeric threshold filtering in Python for victoriametrics queries.

    The prometheus handler cannot filter on numeric values in WHERE (causes 422).
    So we always fetch all rows and filter here before summarisation.

    Returns (filtered_df, threshold_description) where threshold_description is
    a string like "> 50%" to include in the summary prompt, or ("", "") if no
    threshold was found.
    """
    if df is None or df.empty or "value" not in df.columns:
        return df, ""
    q = question.lower()
    # Match patterns like "above 50%", "over 80", "greater than 90%", "more than 70"
    match = re.search(
        r'(?:above|over|greater than|more than|exceed[s]?)\s+(\d+(?:\.\d+)?)\s*%?',
        q,
    )
    if match:
        threshold = float(match.group(1))
        filtered = df[df["value"] > threshold]
        desc = f"above {threshold}%"
        return filtered, desc  # return empty DataFrame if none qualify — triggers "No servers found"
    # Match "below X%" / "under X%" / "less than X%"
    match = re.search(
        r'(?:below|under|less than|lower than)\s+(\d+(?:\.\d+)?)\s*%?',
        q,
    )
    if match:
        threshold = float(match.group(1))
        filtered = df[df["value"] < threshold]
        desc = f"below {threshold}%"
        return filtered, desc  # return empty DataFrame if none qualify
    return df, ""


def _format_metric_answer(question: str, df: "pd.DataFrame", threshold_desc: str) -> str:
    """Build a conversational plain-text answer from victoriametrics data without using the LLM.

    Formats as a proper assistant response with context — not just a raw fact line.
    The LLM is bypassed here because llama3.2 writes preamble and omits the actual data.
    """
    q = question.lower()
    is_anomaly = any(k in q for k in ["anomal", "zscore", "z-score", "behav", "unusual", "abnormal", "misbehav", "weird", "strange", "deviation", "outlier"])
    if "cpu" in q or "processor" in q:
        metric = "CPU usage"
        unit_label = "CPU"
    elif "memory" in q or " mem " in q or "ram" in q:
        metric = "memory usage"
        unit_label = "memory"
    elif "disk" in q or "storage" in q:
        metric = "disk usage"
        unit_label = "disk"
    elif is_anomaly:
        metric = "anomaly detection"
        unit_label = "z-score"
    else:
        metric = "metric value"
        unit_label = "metric"

    # Anomaly queries get their own formatter — z-scores are not percentages
    if is_anomaly and df is not None and not df.empty:
        has_anomaly_name = "anomaly_name" in df.columns
        lines = []
        for _, row in df.iterrows():
            host = row.get("meta_hostname") or row.get("hostname") or row.get("instance") or "unknown"
            val = row.get("value")
            try:
                fval = float(val) if val is not None else None
            except (TypeError, ValueError):
                fval = None
            if fval is None:
                continue
            anomaly_name = row.get("anomaly_name", "") if has_anomaly_name else ""
            label = f"{host} — {anomaly_name}" if anomaly_name else host
            if fval > 3.0:
                flag = " ⚠ CRITICAL (z-score > 3.0)"
            elif fval > 2.0:
                flag = " ⚠ anomalous (z-score > 2.0)"
            else:
                flag = " (normal)"
            lines.append(f"  • {label}: z-score = {fval:.2f}{flag}")

        anomalous = [l for l in lines if "anomalous" in l or "CRITICAL" in l]
        normal = [l for l in lines if "(normal)" in l]

        asking_for_normal = any(k in q for k in ["normal", "healthy", "right", "good", "ok", "fine", "correct", "stable"])
        asking_for_problems = any(k in q for k in ["unusual", "abnormal", "anomal", "behaviour", "behavior", "zscore", "z-score"])

        if asking_for_normal and not asking_for_problems:
            # User wants to know what's healthy
            if not normal:
                return f"All {len(lines)} monitored metric(s) are currently anomalous — none are in the normal range."
            return (
                f"Healthy metrics right now ({len(normal)} out of {len(lines)} monitored, z-score < 2.0):\n\n"
                + "\n".join(normal)
            )

        # Default: show problems (what's wrong / anomalous)
        if not anomalous:
            return (
                f"No anomalous behaviour detected right now across {len(lines)} monitored metric(s). "
                f"All z-scores are below 2.0 (normal range)."
            )
        intro = (
            f"Anomaly detection results — {len(anomalous)} anomalous metric(s) detected "
            f"out of {len(lines)} monitored (z-score > 2.0 = anomalous, > 3.0 = critical):\n\n"
        )
        return intro + "\n".join(anomalous)

    qualifier = f" {threshold_desc}" if threshold_desc else ""

    if df is None or df.empty:
        if threshold_desc:
            return (
                f"I checked the current {metric} across all monitored servers. "
                f"No servers are currently running {threshold_desc} — all hosts are within normal range."
            )
        return f"No {metric} data is currently available from VictoriaMetrics."

    # For disk queries, key by host+mountpoint so each mountpoint is shown separately.
    # For CPU/memory, deduplicate by hostname keeping the peak value (one row per CPU core).
    has_mountpoint = "mountpoint" in df.columns
    host_max: dict = {}
    for _, row in df.iterrows():
        host = (
            row.get("meta_hostname")
            or row.get("hostname")
            or row.get("instance")
            or "unknown"
        )
        if has_mountpoint and row.get("mountpoint"):
            key = f"{host}:{row['mountpoint']}"
        else:
            key = host
        val = row.get("value")
        if val is not None:
            try:
                fval = float(val)
            except (TypeError, ValueError):
                fval = None
            if fval is not None:
                if key not in host_max or fval > host_max[key]:
                    host_max[key] = fval

    if not host_max:
        return f"No {metric} data is currently available from VictoriaMetrics."

    # Detect superlative/single-answer intent — only unambiguous superlatives that imply one result.
    # Deliberately excludes "which servers" (plural), "high", "top N" so those still list all results.
    _superlative_keywords = ("highest", "maximum", "the most", "worst", "max cpu", "max memory", "max disk")
    _asking_for_single = any(kw in q for kw in _superlative_keywords)

    # Sort by value descending, cap at 1 for superlative questions, 20 otherwise
    _cap = 1 if _asking_for_single else 20
    sorted_hosts = sorted(host_max.items(), key=lambda x: x[1], reverse=True)[:_cap]
    count = len(sorted_hosts)

    # Build bullet list
    lines = []
    for host, val in sorted_hosts:
        level = ""
        if val >= 90:
            level = " ⚠ critical"
        elif val >= 75:
            level = " ⚠ high"
        lines.append(f"  • {host}: {val:.1f}%{level}")

    # Count distinct hostnames for the "actively reporting" message
    if has_mountpoint:
        distinct_hosts = len({k.split(":")[0] for k in host_max})
    else:
        distinct_hosts = count
    host_word = "server" if distinct_hosts == 1 else "servers"
    server_word = "server" if count == 1 else "servers"

    if _asking_for_single and not threshold_desc:
        top_host, top_val = sorted_hosts[0]
        level_note = " ⚠ critically high" if top_val >= 90 else (" ⚠ high" if top_val >= 75 else "")
        return (
            f"The server with the highest {metric} right now is **{top_host}** at {top_val:.1f}%{level_note}."
        )
    elif threshold_desc:
        intro = (
            f"I queried current {metric} from VictoriaMetrics. "
            f"Found {count} {server_word} with {unit_label} {threshold_desc} "
            f"({distinct_hosts} {host_word} actively reporting metrics):\n\n"
        )
    else:
        intro = (
            f"Here is the current {metric} from actively monitored servers "
            f"({distinct_hosts} {host_word} reporting metrics to VictoriaMetrics):\n\n"
        )

    body = "\n".join(lines)

    # Add a brief summary note
    top_host, top_val = sorted_hosts[0]
    if threshold_desc and count > 0:
        if top_val >= 90:
            note = f"\n\n{top_host} is at {top_val:.1f}% — this is critically high and may need immediate attention."
        elif top_val >= 75:
            note = f"\n\n{top_host} is the highest at {top_val:.1f}% — worth monitoring."
        else:
            note = f"\n\n{top_host} leads at {top_val:.1f}%."
    else:
        note = ""

    return intro + body + note


def _format_oscardb_answer(question: str, df: "pd.DataFrame") -> str | None:
    """Python formatter for oscar_db results (alerts, tasks, notifications).

    Returns a clean bullet-list answer, bypassing the LLM to avoid verbose prose,
    "some of them" hedging, and commentary about time windows.
    Returns None if the DataFrame does not match a known oscar_db result shape.
    """
    if df is None or df.empty:
        return None

    cols = set(df.columns)

    # ── ALERTS ──────────────────────────────────────────────────────────────
    if "alertname" in cols or "severity" in cols:
        sev_order = _SEVERITY_ORDER
        severity_icons = _SEVERITY_ICONS
        rows = df.to_dict(orient="records")
        count = len(rows)
        if count == 0:
            return "No alerts found matching the query."
        lines = []
        for row in rows:
            name = row.get("alertname") or row.get("name") or "unknown"
            sev = str(row.get("severity") or "").lower()
            icon = severity_icons.get(sev, "⚪")
            summary = row.get("summary") or row.get("description") or ""
            ts = row.get("last_occurrence") or row.get("startsAt") or row.get("timestamp") or ""
            ts_str = f"  last: {ts}" if ts else ""
            sev_label = sev.upper() if sev else "UNKNOWN"
            line = f"  • {icon} [{sev_label}] {name}"
            if summary:
                line += f" — {summary}"
            if ts_str:
                line += ts_str
            lines.append(line)
        # Sort by severity (critical → major → minor → warning → info)
        lines.sort(key=lambda l: sev_order.get(
            next((s for s in sev_order if s in l.lower()), "unknown"), 5
        ))
        return f"Found {count} alert(s):\n\n" + "\n".join(lines)

    # ── TASKS ────────────────────────────────────────────────────────────────
    if "state" in cols and ("task_id" in cols or "name" in cols):
        state_icons = {"success": "✅", "failure": "❌", "failed": "❌", "in_progress": "⏳", "pending": "⏸"}
        rows = df.to_dict(orient="records")
        count = len(rows)
        if count == 0:
            return "No task history found matching the query."
        lines = []
        for row in rows:
            name = row.get("name") or row.get("task_name") or f"task#{row.get('task_id', '?')}"
            state = str(row.get("state") or "").lower()
            icon = state_icons.get(state, "❓")
            result = row.get("result") or ""
            started = row.get("started") or row.get("start_time") or ""
            ts_str = f"  started: {started}" if started else ""
            line = f"  • {icon} [{state.upper()}] {name}"
            if result and str(result) not in ("nan", "None", ""):
                # Truncate very long result messages
                result_str = str(result)[:120] + ("…" if len(str(result)) > 120 else "")
                line += f" — {result_str}"
            if ts_str:
                line += ts_str
            lines.append(line)
        return f"Found {count} task execution(s):\n\n" + "\n".join(lines)

    # ── NOTIFICATIONS ────────────────────────────────────────────────────────
    if "notifier_name" in cols:
        rows = df.to_dict(orient="records")
        count = len(rows)
        if count == 0:
            return "No notification records found matching the query."
        lines = []
        for row in rows:
            notifier = row.get("notifier_name") or "unknown"
            alert = row.get("alert_name") or row.get("alertname") or ""
            status = str(row.get("status") or "").upper()
            retries = row.get("retry_count")
            details = row.get("status_details") or row.get("details") or ""
            line = f"  • [{status}] {notifier}"
            if alert:
                line += f" → {alert}"
            if retries not in (None, "nan", ""):
                line += f" (retries: {retries})"
            if details and str(details) not in ("nan", "None", ""):
                detail_str = str(details)[:100] + ("…" if len(str(details)) > 100 else "")
                line += f" — {detail_str}"
            lines.append(line)
        return f"Found {count} notification record(s):\n\n" + "\n".join(lines)

    return None


def _get_fallback_sql(question: str) -> str:
    """Return a safe fallback SQL for the question if the model-generated SQL fails."""
    q = question.lower()
    if any(k in q for k in ["cpu", "processor", "utilization"]):
        return _EXAMPLE_SQL["cpu"]
    if any(k in q for k in ["memory", " mem ", "ram"]):
        return _EXAMPLE_SQL["memory"]
    if any(k in q for k in ["disk", "storage", "inode"]):
        return _EXAMPLE_SQL["disk"]
    if any(k in q for k in ["anomal", "zscore", "z-score"]):
        return _EXAMPLE_SQL["anomaly"]
    if any(k in q for k in ["alert", "firing", "incident", "acknowledged"]):
        return _EXAMPLE_SQL["alert"]
    if any(k in q for k in ["task", "celery", "job run"]):
        if any(k in q for k in ["running", "in progress", "in_progress", "active", "current"]):
            return _EXAMPLE_SQL["task_running"]
        if any(k in q for k in ["pending", "waiting", "queue"]):
            return _EXAMPLE_SQL["task_pending"]
        return _EXAMPLE_SQL["task"]
    if any(k in q for k in ["notif", "notifier", "email", "webhook", "delivery"]):
        return _EXAMPLE_SQL["notif"]
    return ""


# Suppress asyncio warnings about unretrieved task exceptions from httpx cleanup
# This is a known issue where httpx.AsyncClient tries to close connections after the event loop is closed
warnings.filterwarnings("ignore", message=".*Task exception was never retrieved.*", category=RuntimeWarning)


def langfuse_traced_stream(trace_name="api-completion", span_name="run-completion"):
    """Decorator that wraps a generator method with Langfuse trace/span lifecycle."""

    def decorator(method):
        @functools.wraps(method)
        def wrapper(self, messages, *args, **kwargs):
            # Setup trace & span
            self.langfuse_client_wrapper.setup_trace(
                name=trace_name,
                input=messages,
                tags=self.get_tags(),
                metadata=self.get_metadata(),
                user_id=ctx.user_id,
                session_id=ctx.session_id,
            )
            self.run_completion_span = self.langfuse_client_wrapper.start_span(
                name=span_name,
                input=messages,
            )
            try:
                yield from method(self, messages, *args, **kwargs)
            finally:
                self.langfuse_client_wrapper.end_span(self.run_completion_span)

        return wrapper

    return decorator


class PydanticAIAgent:
    """Pydantic AI-based agent to replace LangchainAgent"""

    def __init__(
        self,
        agent: db.Agents,
        llm_params: dict = None,
    ):
        """
        Initialize Pydantic AI agent.

        Args:
            agent: Agent database record
            args: Agent parameters (optional)
            llm_params: LLM parameters (optional)
        """
        self.agent = agent

        self.run_completion_span: Optional[object] = None
        self.llm: Optional[object] = None
        self.embedding_model: Optional[object] = None

        self.log_callback_handler: Optional[object] = None
        self.langfuse_callback_handler: Optional[object] = None
        self.mdb_langfuse_callback_handler: Optional[object] = None

        self.langfuse_client_wrapper = LangfuseClientWrapper()
        self.agent_mode = self.agent.params.get("mode", "text")

        self.llm_params = llm_params

        # Env var override: KORE_DEFAULT_LLM_* take priority over the agent DB config.
        # This lets you switch models by editing overrides.env + restarting kore,
        # without needing to call the REST API or touch the MindsDB DB.
        #
        # overrides.env example:
        #   KORE_DEFAULT_LLM_MODEL_NAME=qwen2.5:3b
        #   KORE_DEFAULT_LLM_PROVIDER=ollama
        #   KORE_DEFAULT_LLM_API_KEY=ollama
        #   KORE_DEFAULT_LLM_BASE_URL=http://172.18.0.1:11435/v1
        #
        # To use GPT-4o-mini instead:
        #   KORE_DEFAULT_LLM_MODEL_NAME=gpt-4o-mini
        #   KORE_DEFAULT_LLM_PROVIDER=openai
        #   KORE_DEFAULT_LLM_API_KEY=sk-...
        #   KORE_DEFAULT_LLM_BASE_URL=          (leave empty for OpenAI default)
        import os as _os
        _env_model    = _os.getenv("KORE_DEFAULT_LLM_MODEL_NAME", "").strip()
        _env_provider = _os.getenv("KORE_DEFAULT_LLM_PROVIDER",   "").strip()
        _env_api_key  = _os.getenv("KORE_DEFAULT_LLM_API_KEY",    "").strip()
        _env_base_url = _os.getenv("KORE_DEFAULT_LLM_BASE_URL",   "").strip()
        if _env_model:
            self.llm_params = dict(self.llm_params or {})
            self.llm_params["model_name"] = _env_model
            if _env_provider:
                self.llm_params["provider"] = _env_provider
            if _env_api_key:
                self.llm_params["api_key"] = _env_api_key
            if _env_base_url:
                self.llm_params["openai_api_base"] = _env_base_url
                self.llm_params["ollama_base_url"]  = _env_base_url
            else:
                # Clear any stale base_url from DB when env var is blank (e.g. switching to OpenAI)
                self.llm_params.pop("openai_api_base", None)
                self.llm_params.pop("ollama_base_url",  None)
            logger.info(
                "[KORE] Model from env: model=%s provider=%s",
                _env_model, self.llm_params.get("provider", "?")
            )

        # Provider model instance
        self.model_instance = get_model_instance_from_kwargs(self.llm_params)

        # Command executor for queries
        tables_list = self.agent.params.get("data", {}).get("tables", [])
        knowledge_bases_list = self.agent.params.get("data", {}).get("knowledge_bases", [])
        self.sql_toolkit = MindsDBQuery(tables_list, knowledge_bases_list)

        import os
        _pm_slug       = self.agent.params.get("prompt_slug", os.getenv("KORE_PROMPT_SLUG", "oscar-ops-agent"))
        _pm_env        = self.agent.params.get("prompt_env",  os.getenv("KORE_PROMPT_ENV",  "production"))
        # Fallback prompt used when prompt_template is absent from agent params AND prompt manager
        # is unreachable.  This should never be hit in practice — the agent should always be
        # created with an explicit prompt_template (see AGENT_GUIDE.md § "How to Create the Agent").
        _DEFAULT_FALLBACK_PROMPT = (
            "You are OSCAR Ops Assistant. Answer IT operations questions by writing SQL.\n"
            "ROUTING: victoriametrics = live metrics | oscar_db = alerts/tasks/notifications\n"
            "RULES: always use table aliases (h.state not state); victoriametrics needs WHERE time = 'now';\n"
            "task join: TM_History h JOIN TM_Tasks t ON h.task_id = t.id;\n"
            "firing alerts: add last_occurrence > DATE_SUB(NOW(), INTERVAL 24 HOUR);\n"
            "task states: SUCCESS/FAILURE/FAILED/PENDING/IN_PROGRESS/SUBMITTED.\n"
            "Set type=final_query, put SQL in sql_query."
        )
        _pm_fallback   = self.agent.params.get("prompt_template", _DEFAULT_FALLBACK_PROMPT)
        _pm_model_name = self.llm_params.get("model_name", "") if self.llm_params else ""
        self.system_prompt = _fetch_prompt_from_manager(
            slug=_pm_slug,
            environment=_pm_env,
            fallback=_pm_fallback,
            model_name=_pm_model_name,
        )

        # Track current query state
        self._current_prompt: Optional[str] = None
        self._current_sql_query: Optional[str] = None
        self._current_query_result: Optional[pd.DataFrame] = None

        self.select_targets = None

    def _convert_messages_to_history(self, df: pd.DataFrame, args: dict) -> List[ModelMessage]:
        """
        Convert DataFrame messages to Pydantic AI message history format.

        Args:
            df: DataFrame with user/assistant columns or role/content columns

        Returns:
            List of Pydantic AI Message objects
        """
        messages = []

        # Check if DataFrame has 'role' and 'content' columns (API format)
        if "role" in df.columns and "content" in df.columns:
            for _, row in df.iterrows():
                role = row.get("role")
                content = row.get("content", "")
                if pd.notna(role) and pd.notna(content):
                    if role == "user":
                        messages.append(ModelRequest.user_text_prompt(str(content)))
                    elif role == "assistant":
                        messages.append(ModelResponse(parts=[TextPart(content=str(content))]))
        else:
            # Legacy format with question/answer columns
            user_column = args.get("user_column", USER_COLUMN)
            assistant_column = args.get("assistant_column", ASSISTANT_COLUMN)

            for _, row in df.iterrows():
                user_msg = row.get(user_column)
                assistant_msg = row.get(assistant_column)

                if pd.notna(user_msg) and str(user_msg).strip():
                    messages.append(ModelRequest.user_text_prompt(str(user_msg)))

                if pd.notna(assistant_msg) and str(assistant_msg).strip():
                    messages.append(ModelResponse(parts=[TextPart(content=str(assistant_msg))]))

        return messages

    def _extract_current_prompt_and_history(self, messages: Any, args: Dict) -> tuple[str, List[ModelMessage]]:
        """
        Extract current prompt and message history from messages in various formats.

        Args:
            messages: Can be:
                - List of dicts with 'role' and 'content' (API format)
                - List of dicts with 'question' and 'answer' (Q&A format from A2A)
                - DataFrame with 'role'/'content' columns (API format)
                - DataFrame with 'question'/'answer' columns (legacy format)
            args: Arguments dict

        Returns:
            Tuple of (current_prompt: str, message_history: List[ModelMessage])
        """
        # Handle list of dicts with 'role' and 'content' (API format)
        if isinstance(messages, list) and len(messages) > 0:
            if isinstance(messages[0], dict) and "role" in messages[0]:
                # Convert to Pydantic AI Message objects
                pydantic_messages = []
                for msg in messages:
                    if msg.get("role") == "user":
                        pydantic_messages.append(ModelRequest.user_text_prompt(msg.get("content", "")))
                    elif msg.get("role") == "assistant":
                        pydantic_messages.append(ModelResponse(parts=[TextPart(content=msg.get("content", ""))]))

                # Get current prompt (last user message)
                current_prompt = ""
                for msg in reversed(messages):
                    if msg.get("role") == "user":
                        current_prompt = msg.get("content", "")
                        break

                # Get message history (all except last message)
                message_history = pydantic_messages[:-1] if len(pydantic_messages) > 1 else []
                return current_prompt, message_history

            # Handle Q&A format (from A2A conversion): list of dicts with 'question' and 'answer' keys
            elif isinstance(messages[0], dict) and "question" in messages[0]:
                # Convert Q&A format to role/content format for processing
                role_content_messages = []
                for qa_msg in messages:
                    question = qa_msg.get("question", "")
                    answer = qa_msg.get("answer", "")

                    # Add user message (question)
                    if question:
                        role_content_messages.append({"role": "user", "content": str(question)})

                    # Add assistant message (answer) if present
                    if answer:
                        role_content_messages.append({"role": "assistant", "content": str(answer)})

                # Now process as role/content format
                if len(role_content_messages) > 0:
                    pydantic_messages = []
                    for msg in role_content_messages:
                        if msg.get("role") == "user":
                            pydantic_messages.append(ModelRequest.user_text_prompt(msg.get("content", "")))
                        elif msg.get("role") == "assistant":
                            pydantic_messages.append(ModelResponse(parts=[TextPart(content=msg.get("content", ""))]))

                    # Get current prompt (last user message)
                    current_prompt = ""
                    for msg in reversed(role_content_messages):
                        if msg.get("role") == "user":
                            current_prompt = msg.get("content", "")
                            break

                    # Get message history (all except last message)
                    message_history = pydantic_messages[:-1] if len(pydantic_messages) > 1 else []
                    return current_prompt, message_history

        # Handle DataFrame format
        df = messages if isinstance(messages, pd.DataFrame) else pd.DataFrame(messages)
        df = df.reset_index(drop=True)

        # Check if DataFrame has 'role' and 'content' columns (API format)
        if "role" in df.columns and "content" in df.columns:
            # Convert to Pydantic AI Message objects
            pydantic_messages = []
            for _, row in df.iterrows():
                role = row.get("role")
                content = row.get("content", "")
                if pd.notna(role) and pd.notna(content):
                    if role == "user":
                        pydantic_messages.append(ModelRequest.user_text_prompt(str(content)))
                    elif role == "assistant":
                        pydantic_messages.append(ModelResponse(parts=[TextPart(content=str(content))]))

            # Get current prompt (last user message)
            current_prompt = ""
            for index in reversed(range(len(df))):
                row = df.iloc[index]
                if row.get("role") == "user":
                    current_prompt = str(row.get("content", ""))
                    break

            # Get message history (all except last message)
            message_history = pydantic_messages[:-1] if len(pydantic_messages) > 1 else []
            return current_prompt, message_history

        # Legacy DataFrame format with question/answer columns
        user_column = args.get("user_column", USER_COLUMN)
        current_prompt = ""
        if len(df) > 0 and user_column in df.columns:
            user_messages = df[user_column].dropna()
            if len(user_messages) > 0:
                current_prompt = str(user_messages.iloc[-1])

        # Convert history (all except last)
        history_df = df[:-1] if len(df) > 1 else pd.DataFrame()
        message_history = self._convert_messages_to_history(history_df, args)
        return current_prompt, message_history

    def get_metadata(self) -> Dict:
        """Get metadata for observability"""
        return {
            "model_name": self.llm_params["model_name"],
            "user_id": ctx.user_id,
            "session_id": ctx.session_id,
            "company_id": ctx.company_id,
            "user_class": ctx.user_class,
        }

    def get_tags(self) -> List:
        """Get tags for observability"""
        return ["AGENT", "PYDANTIC_AI"]

    def get_select_targets_from_sql(self, sql) -> Optional[List[str]]:
        """
        Get the SELECT targets from the original SQL query if available.
        Extracts only the column names, ignoring aliases (e.g., "col1 as alias" -> "col1").

        Returns:
            List of SELECT target column names if available, None otherwise
        """

        try:
            parsed = parse_sql(sql)
        except Exception:
            return

        if not isinstance(parsed, ast.Select):
            return

        targets = []
        for target in parsed.targets:
            if isinstance(target, ast.Identifier):
                targets.append(target.parts[-1])

            elif isinstance(target, ast.Star):
                return  # ['question', 'answer']

            elif isinstance(target, ast.Function):
                # For functions, get the function name and args
                func_str = target.op
                targets.append(func_str)
                if target.args:
                    for arg in target.args:
                        if isinstance(arg, ast.Identifier):
                            targets.append(arg.parts[-1])

        return targets

    def get_completion(self, messages, stream: bool = False, params: dict | None = None):
        """
        Get completion from agent.

        Args:
            messages: List of message dictionaries or DataFrame
            stream: Whether to stream the response
            params: Additional parameters

        Returns:
            DataFrame with assistant response
        """
        # Extract SQL context from params if present
        if params and "original_query" in params:
            original_query = params.pop("original_query")

            self.select_targets = self.get_select_targets_from_sql(original_query)

        args = {}
        args.update(self.agent.params or {})
        args.update(params or {})

        data = None
        if stream:
            return self._get_completion_stream(messages, args)
        else:
            for message in self._get_completion_stream(messages, args):
                if message.get("type") == "end":
                    break
                elif message.get("type") == "error":
                    error_message = f"Agent failed with model error: {message.get('content')}"
                    raise RuntimeError(error_message)
                last_message = message

                # if last_message.get("type") == "sql":
                #     sql_query = last_message.get("content")

                if last_message.get("type") == "data":
                    if "text" in last_message:
                        data = pd.DataFrame([{"answer": last_message["text"]}])
                    else:
                        data = last_message.get("content")

            else:
                error_message = f"Agent failed with model error: {last_message.get('content')}"
                return self._create_error_response(error_message, return_context=params.get("return_context", True))

            # Validate select targets if specified

            if self.select_targets is not None:
                # Ensure all expected columns are present
                if data is None or (isinstance(data, pd.DataFrame) and data.empty):
                    # Create DataFrame with one row of nulls for all expected columns
                    data = pd.DataFrame({col: [None] for col in self.select_targets})
                else:
                    # Ensure all expected columns exist, add missing ones with null values
                    cols_map = {c.lower(): c for c in data.columns}

                    for col in self.select_targets:
                        if col not in data.columns:
                            # try to find case independent
                            if col.lower() in cols_map:
                                data[col] = data[cols_map[col.lower()]]
                            else:
                                data[col] = None
                    # Reorder columns to match select_targets order
                    data = data[self.select_targets]

            if data is not None and isinstance(data, pd.DataFrame) and TRACE_ID_COLUMN not in data.columns:
                data[TRACE_ID_COLUMN] = self.langfuse_client_wrapper.get_trace_id()
            return data

    def _create_error_response(self, error_message: str, return_context: bool = True) -> pd.DataFrame:
        """Create error response DataFrame"""
        response_data = {
            ASSISTANT_COLUMN: [error_message],
            TRACE_ID_COLUMN: [self.langfuse_client_wrapper.get_trace_id()],
        }
        if return_context:
            response_data[CONTEXT_COLUMN] = [json.dumps([])]
        return pd.DataFrame(response_data)

    @langfuse_traced_stream(trace_name="api-completion", span_name="run-completion")
    def _get_completion_stream(self, messages: List[dict], params) -> Iterable[Dict]:
        """
        Get completion as a stream of chunks.

        Args:
            messages: List of message dictionaries or DataFrame

        Returns:
            Iterator of chunk dictionaries
        """
        DEBUG_LOGGER(f"PydanticAIAgent._get_completion_stream: Messages: {messages}")

        # Extract current prompt and message history from messages
        # This handles multiple formats: list of dicts, DataFrame with role/content, or legacy DataFrame
        current_prompt, message_history = self._extract_current_prompt_and_history(messages, params)
        DEBUG_LOGGER(
            f"PydanticAIAgent._get_completion_stream: Extracted prompt and {len(message_history)} history messages"
        )

        yield self._add_chunk_metadata({"type": "status", "content": "Generating Data Catalog..."})

        if self.agent_mode == "text":
            agent_prompts = text_sql_mode
            AgentResponse = text_sql_mode.AgentResponse
        else:
            agent_prompts = sql_mode
            AgentResponse = sql_mode.AgentResponse

        if self.sql_toolkit.knowledge_bases:
            sql_instructions = f"{agent_prompts.sql_description}\n\n{agent_prompts.sql_with_kb_description}"
        else:
            sql_instructions = agent_prompts.sql_description

        data_catalog = _build_oscar_data_catalog()

        # Initialize counters and accumulators
        exploratory_query_count = 0
        exploratory_query_results = []
        MAX_EXPLORATORY_QUERIES = 20
        MAX_RETRIES = 3

        # Planning step: Create a plan before generating queries
        yield self._add_chunk_metadata({"type": "status", "content": "Creating execution plan..."})

        # Build planning prompt
        planning_prompt_text = f"""Take into account the following Data Catalog:\n{data_catalog}\n\n{agent_prompts.planning_prompt}\n\nQuestion to answer: {current_prompt}"""
        DEBUG_LOGGER(f"PydanticAIAgent._get_completion_stream: Planning prompt text: {planning_prompt_text}")
        # Get select targets for planning context

        select_targets_str = None
        if self.select_targets is not None:
            select_targets_str = ", ".join(str(t) for t in self.select_targets)
            planning_prompt_text += f"\n\nFor the final query, the user expects to have a table such that this query is valid: SELECT {select_targets_str} FROM (<generated query>); when creating your plan, make sure to account for these expected columns."

        # Generate plan.
        # Use _direct_llm_json_call (response_format=json_schema) for all providers — it is
        # reliable across Ollama, vLLM, and OpenAI-compatible endpoints.  The previous approach
        # of using pydantic-ai Agent with output_type=PlanResponse relied on tool-calling, which
        # causes Ollama to return assistant messages with content=null.  When pydantic-ai retried
        # (retries=5), each attempt burned a full inference round-trip (~7s) before the 400 was
        # raised, totalling ~35s of wasted time per question — all before the real query started.
        _planning_system = "You are a data analyst. Create a brief query plan. Respond only with the required JSON fields."
        try:
            raw_plan = _direct_llm_json_call(self.llm_params, _planning_system, planning_prompt_text, schema=_PLANNING_SCHEMA)
            plan = PlanResponse(
                plan=raw_plan.get("plan", "Query the relevant tables directly."),
                estimated_steps=int(raw_plan.get("estimated_steps", 1)),
            )
        except Exception as plan_err:
            logger.warning(f"Planning step failed ({plan_err}), using default plan.")
            plan = PlanResponse(
                plan="Query the relevant tables directly to answer the question in one step.",
                estimated_steps=1,
            )
        # Validate plan steps don't exceed MAX_EXPLORATORY_QUERIES
        if plan.estimated_steps > MAX_EXPLORATORY_QUERIES:
            logger.warning(
                f"Plan estimated {plan.estimated_steps} steps, but maximum is {MAX_EXPLORATORY_QUERIES}. Adjusting plan."
            )
            plan.plan += (
                f"\n\nNote: The plan has been adjusted to ensure it does not exceed {MAX_EXPLORATORY_QUERIES} steps."
            )

        DEBUG_LOGGER(f"Generated plan with {plan.estimated_steps} estimated steps: {plan.plan}")

        # Yield the plan as a status message
        yield self._add_chunk_metadata(
            {
                "type": "status",
                "content": f"Proposed Execution Plan:\n{plan.plan}\n\nEstimated steps: {plan.estimated_steps}\n\n",
            }
        )

        # Save original question before current_prompt gets overwritten in the loop below
        original_question = current_prompt

        # Build base prompt with plan included
        base_prompt = f"\n\nTake into account the following Data Catalog:\n{data_catalog}\nMindsDB SQL instructions:\n{sql_instructions}\n\nProposed Execution Plan:\n{plan.plan}\n\nEstimated steps: {plan.estimated_steps} (maximum allowed: {MAX_EXPLORATORY_QUERIES})\n\nPlease follow this plan and write Mindsdb SQL queries to answer the question:\n{current_prompt}"

        if select_targets_str is not None:
            base_prompt += f"\n\nFor the final query the user expects to have a table such that this query is valid: SELECT {select_targets_str} FROM (<generated query>); when generating the SQL query make sure to include those columns, do not fix grammar on columns. Keep them as the user wants them"

        DEBUG_LOGGER(
            f"PydanticAIAgent._get_completion_stream: Sending LLM request with Current prompt: {current_prompt}"
        )
        DEBUG_LOGGER(f"PydanticAIAgent._get_completion_stream: Message history: {message_history}")

        # For providers where pydantic-ai tool-calling is unreliable (Ollama and compatible
        # endpoints), skip the Agent entirely.  pydantic-ai uses tool-calling to enforce
        # structured output, but Ollama returns assistant messages with content=null after a
        # tool call.  When pydantic-ai retries on validation failure it sends that null-content
        # message back, and Ollama returns 400 'invalid message content type: <nil>'.  Each
        # retry burns a full inference round-trip (~15-35s), totalling ~35s wasted before the
        # exception is raised and we fall through to _direct_llm_json_call anyway.
        # _direct_llm_json_call uses response_format=json_schema which Ollama handles correctly
        # on the first attempt — no retries, no null-content issues.
        _skip_pydantic_agent = _is_direct_json_provider(self.llm_params)
        agent = None if _skip_pydantic_agent else Agent(self.model_instance, system_prompt=self.system_prompt, output_type=AgentResponse)

        retry_count = 0

        try:
            while True:
                yield self._add_chunk_metadata({"type": "status", "content": "Generating agent response..."})

                current_prompt = base_prompt
                if exploratory_query_results:
                    current_prompt += "\n\nPrevious exploratory query results:\n" + "\n---\n".join(
                        exploratory_query_results
                    )

                if exploratory_query_count == MAX_EXPLORATORY_QUERIES:
                    current_prompt += f"\n\nIMPORTANT: You have reached the maximum number of exploratory queries ({MAX_EXPLORATORY_QUERIES}). The next query you generate MUST be a final_query or final_text."

                try:
                    if _skip_pydantic_agent:
                        raise RuntimeError("Direct-JSON provider: bypassing pydantic-ai structured output")
                    result = agent.run_sync(
                        current_prompt,
                        message_history=message_history if message_history else None,
                    )
                    output = result.output
                except Exception as agent_err:
                    # pydantic-ai tool-calling failed (common with small models like llama3.2
                    # that don't reliably call the final_result tool). Fall back to a direct
                    # response_format=json_schema call which Ollama handles correctly.
                    if not _skip_pydantic_agent:
                        logger.warning(f"pydantic-ai agent failed ({agent_err}), falling back to direct JSON call")
                    prev = ("\n\nPrevious query results:\n" + "\n---\n".join(exploratory_query_results)) if exploratory_query_results else ""
                    routing_hint = _get_routing_hint(original_question)
                    logger.info(f"[KORE ROUTE] routing_hint={'HEALTH_SUMMARY' if routing_hint == _HEALTH_SUMMARY_SENTINEL else ('EMPTY' if not routing_hint else routing_hint.splitlines()[0])}")
                    if routing_hint == _HEALTH_SUMMARY_SENTINEL:
                        # Health summary: run 3 pre-built queries (alerts, tasks, CPU) and feed
                        # all results to the summarizer in a single LLM call.
                        summary_sections = []
                        for label, key in [("Firing alerts", "health_alerts"), ("Recent task failures", "health_tasks"), ("Top CPU servers", "health_cpu")]:
                            try:
                                df = self.sql_toolkit.execute_sql(_EXAMPLE_SQL[key], escape_identifiers=True)
                                rows = []
                                for i, row in enumerate(df.head(10).to_dict(orient="records"), 1):
                                    fields = ", ".join(f"{k}={v}" for k, v in row.items() if v is not None and str(v) != "nan")
                                    rows.append(f"  {i}. {fields}")
                                section = f"{label} ({len(rows)} rows):\n" + ("\n".join(rows) if rows else "  No data.")
                            except Exception as _se:
                                section = f"{label}: query failed ({_se})"
                            summary_sections.append(section)
                        combined = "\n\n".join(summary_sections)
                        # Return combined data directly — small models like llama3.2 struggle to
                        # summarise 3 sections in one call, so we format clearly and skip the
                        # extra LLM round-trip.  Larger models get a proper summary via the main
                        # pydantic-ai path and never reach this fallback.
                        output = AgentResponse(sql_query="", text=combined, short_description="health summary", type="final_text")
                    elif _get_fallback_sql(original_question) and any(
                        k in original_question.lower()
                        for k in ["cpu", "processor", "utilization", "memory", " mem ", "ram", "disk", "storage", "inode"]
                    ):
                        # DIRECT METRIC PATH — skip LLM SQL generation entirely for cpu/memory/disk.
                        # We know the exact SQL to run; no need to ask the model. This eliminates
                        # the entire class of "model generates wrong table name / adds value filter"
                        # bugs for metric questions.
                        direct_sql = _get_fallback_sql(original_question)
                        try:
                            sql_result = self.sql_toolkit.execute_sql(direct_sql, escape_identifiers=True)
                            sql_result, threshold_desc = _apply_value_threshold(original_question, sql_result)
                            answer_text = _format_metric_answer(original_question, sql_result, threshold_desc)
                        except Exception as metric_err:
                            logger.warning(f"Direct metric SQL failed ({metric_err})")
                            answer_text = f"Could not retrieve metric data: {metric_err}"
                        output = AgentResponse(
                            sql_query=direct_sql,
                            text=answer_text,
                            short_description="",
                            type="final_text",
                        )
                    elif routing_hint:
                        # Focused mode for oscar_db questions (alerts, tasks, notifications).
                        # Provide only the routing hint — skip the full 14-table data catalog.
                        focused_user = f"{routing_hint}{prev}\nQuestion: {original_question}"
                        raw = _direct_llm_json_call(self.llm_params, self.system_prompt, focused_user)
                        output = AgentResponse(**raw)
                    else:
                        focused_user = f"{data_catalog}{prev}\n\nQuestion: {original_question}"
                        raw = _direct_llm_json_call(self.llm_params, self.system_prompt, focused_user)
                        output = AgentResponse(**raw)

                    # For text mode: if the model chose a data query type, run the SQL and do a
                    # second LLM call to summarise the results into natural language.  Without
                    # this, raw SQL results are returned but the caller expects an "answer" column.
                    if self.agent_mode == "text" and output.type in ("final_query", "exploratory_query") and output.sql_query:
                        try:
                            # Strip any non-SQL prefix the model may have prepended (e.g. "Use this SQL:\n")
                            # by finding the first SELECT/WITH/INSERT keyword.
                            _sql_match = re.search(r'\b(SELECT|WITH|INSERT|UPDATE|DELETE)\b', output.sql_query, re.IGNORECASE)
                            clean_sql = output.sql_query[_sql_match.start():].strip() if _sql_match else output.sql_query
                            sql_result = self.sql_toolkit.execute_sql(clean_sql, escape_identifiers=True)
                            # Apply Python-side threshold filtering for victoriametrics queries.
                            # The prometheus handler cannot filter on numeric values in WHERE
                            # (causes 422), so we fetch all rows then filter here.
                            sql_result, threshold_desc = _apply_value_threshold(original_question, sql_result)
                            # If the result has a 'value' column it is victoriametrics metric data.
                            # llama3.2 cannot reliably summarise structured rows — use Python formatter.
                            is_metric_result = (
                                sql_result is not None
                                and not sql_result.empty
                                and "value" in sql_result.columns
                            )
                            if is_metric_result or (threshold_desc and (sql_result is None or sql_result.empty)):
                                answer_text = _format_metric_answer(original_question, sql_result, threshold_desc)
                                output = AgentResponse(
                                    sql_query=output.sql_query,
                                    text=answer_text,
                                    short_description=output.short_description,
                                    type="final_text",
                                )
                            else:
                                # Non-metric result (oscar_db tables) — use Python formatter first,
                                # fall back to LLM only if no structured formatter matched.
                                oscardb_text = _format_oscardb_answer(original_question, sql_result)
                                if oscardb_text is not None:
                                    output = AgentResponse(
                                        sql_query=output.sql_query,
                                        text=oscardb_text,
                                        short_description=output.short_description,
                                        type="final_text",
                                    )
                                else:
                                    result_lines = []
                                    for i, row in enumerate(sql_result.head(20).to_dict(orient="records"), 1):
                                        fields = ", ".join(f"{k}={v}" for k, v in row.items() if v is not None and str(v) != "nan")
                                        result_lines.append(f"Row {i}: {fields}")
                                    result_plain = "\n".join(result_lines) if result_lines else "No rows returned."
                                    summary_user = (
                                        f"Question: {original_question}\n\n"
                                        f"Data from the database ({len(result_lines)} rows):\n{result_plain}\n\n"
                                        "Using ONLY the data above, write a clear natural language answer. "
                                        "Include the specific names and values from the rows."
                                    )
                                    summary_raw = _direct_llm_json_call(
                                        self.llm_params,
                                        "You are a helpful IT operations assistant. Answer clearly and concisely.",
                                        summary_user,
                                        schema=_SUMMARY_SCHEMA,
                                    )
                                    output = AgentResponse(
                                        sql_query=output.sql_query,
                                        text=summary_raw.get("answer", result_plain),
                                        short_description=output.short_description,
                                        type="final_text",
                                    )
                        except Exception as exec_err:
                            logger.warning(f"Fallback SQL execution failed ({exec_err}), trying example SQL")
                            rescue_sql = _get_fallback_sql(original_question)
                            if rescue_sql and rescue_sql != clean_sql:
                                try:
                                    sql_result = self.sql_toolkit.execute_sql(rescue_sql, escape_identifiers=True)
                                    # Use Python formatter for metric results, LLM for oscar_db results
                                    if sql_result is not None and not sql_result.empty and "value" in sql_result.columns:
                                        sql_result, threshold_desc = _apply_value_threshold(original_question, sql_result)
                                        answer_text = _format_metric_answer(original_question, sql_result, threshold_desc)
                                        output = AgentResponse(
                                            sql_query=rescue_sql,
                                            text=answer_text,
                                            short_description=output.short_description,
                                            type="final_text",
                                        )
                                    else:
                                        oscardb_text = _format_oscardb_answer(original_question, sql_result)
                                        if oscardb_text is not None:
                                            output = AgentResponse(
                                                sql_query=rescue_sql,
                                                text=oscardb_text,
                                                short_description=output.short_description,
                                                type="final_text",
                                            )
                                        else:
                                            result_lines = []
                                            for i, row in enumerate(sql_result.head(20).to_dict(orient="records"), 1):
                                                fields = ", ".join(f"{k}={v}" for k, v in row.items() if v is not None and str(v) != "nan")
                                                result_lines.append(f"Row {i}: {fields}")
                                            result_plain = "\n".join(result_lines) if result_lines else "No rows returned."
                                            summary_user = (
                                                f"Question: {original_question}\n\n"
                                                f"Data from the database ({len(result_lines)} rows):\n{result_plain}\n\n"
                                                "Using ONLY the data above, write a clear natural language answer. "
                                                "Include the specific names and values from the rows."
                                            )
                                            summary_raw = _direct_llm_json_call(
                                                self.llm_params,
                                                "You are a helpful IT operations assistant. Answer clearly and concisely.",
                                                summary_user,
                                                schema=_SUMMARY_SCHEMA,
                                            )
                                            output = AgentResponse(
                                                sql_query=rescue_sql,
                                                text=summary_raw.get("answer", result_plain),
                                                short_description=output.short_description,
                                                type="final_text",
                                            )
                                except Exception as rescue_err:
                                    logger.warning(f"Rescue SQL also failed ({rescue_err}), returning no-data response")
                                    output = AgentResponse(
                                        sql_query=rescue_sql,
                                        text="No data found or query failed.",
                                        short_description=output.short_description,
                                        type="final_text",
                                    )
                            else:
                                output = AgentResponse(
                                    sql_query=output.sql_query,
                                    text="No data found or query failed.",
                                    short_description=output.short_description,
                                    type="final_text",
                                )

                # Extract output

                # Yield description before SQL query
                if output.short_description:
                    yield self._add_chunk_metadata({"type": "context", "content": output.short_description})

                if output.type == ResponseType.FINAL_TEXT:
                    yield self._add_chunk_metadata({"type": "status", "content": "Returning text response"})

                    # return text to user and exit
                    yield self._add_chunk_metadata({"type": "data", "text": output.text})
                    yield self._add_chunk_metadata({"type": "end"})
                    return
                elif output.type == ResponseType.EXPLORATORY and exploratory_query_count == MAX_EXPLORATORY_QUERIES:
                    raise RuntimeError(
                        "Agent exceeded the maximum number of exploratory queries "
                        f"({MAX_EXPLORATORY_QUERIES}) but result still not returned. "
                        f"output.type='{output.type}', expected 'final_query' or 'final_text'."
                    )

                sql_query = output.sql_query
                logger.info(f"[KORE SQL] type={output.type} sql={sql_query!r}")

                try:
                    query_type = "final" if output.type == ResponseType.FINAL_QUERY else "exploratory"
                    yield self._add_chunk_metadata(
                        {"type": "status", "content": f"Executing {query_type} SQL query: {sql_query}"}
                    )
                    query_data = self.sql_toolkit.execute_sql(sql_query, escape_identifiers=True)
                except Exception as e:
                    # Extract error message - prefer db_error_msg for QueryError, otherwise use str(e)
                    query_error = str(e)

                    # Yield descriptive error message
                    error_message = f"Error executing SQL query: {query_error}"
                    yield self._add_chunk_metadata({"type": "status", "content": error_message})

                    retry_count += 1
                    if retry_count >= MAX_RETRIES:
                        DEBUG_LOGGER(
                            f"PydanticAIAgent._get_completion_stream: retry ({retry_count}/{MAX_RETRIES}) after error: {query_error}"
                        )
                        raise RuntimeError(
                            f"Failed to execute {query_type} SQL query after {retry_count} consecutive unsuccessful SQL queries. "
                            f"Last error: {query_error}\nSQL:\n{sql_query}"
                        )

                    query_result_str = f"Query: {sql_query}\nError: {query_error}"
                    exploratory_query_results.append(query_result_str)

                    continue

                DEBUG_LOGGER("PydanticAIAgent._get_completion_stream: Executed SQL query successfully")
                retry_count = 0

                if output.type == ResponseType.FINAL_QUERY:
                    # return response to user
                    yield self._add_chunk_metadata({"type": "data", "content": query_data})
                    yield self._add_chunk_metadata({"type": "end"})
                    return

                # is exploratory
                exploratory_query_count += 1
                debug_message = f"Exploratory query {exploratory_query_count}/{MAX_EXPLORATORY_QUERIES} succeeded"
                DEBUG_LOGGER(debug_message)
                yield self._add_chunk_metadata({"type": "status", "content": debug_message})

                # Format query result for prompt
                markdown_table = dataframe_to_markdown(query_data)
                query_result_str = (
                    f"Query: {sql_query}\nDescription: {output.short_description}\nResult:\n{markdown_table}"
                )
                yield self._add_chunk_metadata({"type": "status", "content": f"Query result: {markdown_table}"})
                exploratory_query_results.append(query_result_str)

        except Exception as e:
            # Suppress the "Event loop is closed" error from httpx cleanup
            # This is a known issue where async HTTP clients try to close after the event loop is closed
            error_msg = str(e)
            if "Event loop is closed" in error_msg:
                # This is a cleanup issue, not a critical error - log at debug level
                DEBUG_LOGGER(f"Async cleanup warning (non-critical): {error_msg}")
            else:
                # Extract error message - prefer db_error_msg for QueryError, otherwise use str(e)
                from mindsdb.utilities.exception import QueryError

                if isinstance(e, QueryError):
                    error_content = e.db_error_msg or str(e)
                    descriptive_error = f"Database query error: {error_content}"
                    if e.failed_query:
                        descriptive_error += f"\n\nFailed query: {e.failed_query}"
                else:
                    error_content = error_msg
                    descriptive_error = f"Agent streaming failed: {error_content}"

                logger.error(f"Agent streaming failed: {error_content}")
                error_chunk = self._add_chunk_metadata(
                    {
                        "type": "error",
                        "content": descriptive_error,
                    }
                )
                yield error_chunk

    def _add_chunk_metadata(self, chunk: Dict) -> Dict:
        """Add metadata to chunk"""
        chunk["trace_id"] = self.langfuse_client_wrapper.get_trace_id()
        return chunk
