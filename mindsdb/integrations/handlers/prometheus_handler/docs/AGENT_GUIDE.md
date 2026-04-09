# OSCAR Kore — NL→SQL Agent: Complete Technical Guide

> **Status:** Phase 2 complete + performance hardened (2026-04-05).
> **Last updated:** 2026-04-05

---

## Table of Contents

1. [What Was Built](#1-what-was-built)
2. [How It Works — Architecture](#2-how-it-works--architecture)
3. [The Two Datasources](#3-the-two-datasources)
4. [The 14 Curated Tables](#4-the-14-curated-tables)
5. [Agent Configuration (current running state)](#5-agent-configuration-current-running-state)
6. [How to Create or Update the Agent](#6-how-to-create-or-update-the-agent)
7. [LLM Provider Reference — Swap Without Recompiling](#7-llm-provider-reference--swap-without-recompiling)
8. [Validation Queries](#8-validation-queries)
9. [Debugging](#9-debugging)
10. [Small Model / Ollama Execution Path](#10-small-model--ollama-execution-path)
11. [Per-Model Prompts via Prompt Manager](#11-per-model-prompts-via-prompt-manager)
12. [What Changes Per Environment vs What Never Changes](#12-what-changes-per-environment-vs-what-never-changes)
13. [Phase Roadmap](#13-phase-roadmap)

---

## 1. What Was Built

OSCAR Kore is a MindsDB instance with a custom pydantic-ai agent that answers natural language questions about IT operations by generating SQL, executing it against real datasources, and returning a natural language summary.

### What was completed in Phase 2:

| Item | Detail |
|------|--------|
| `oscar_ops_agent` | Live NL→SQL agent with 14 curated tables |
| Two datasources | `victoriametrics` (25 tables, live metrics) + `oscar_db` (80+ MySQL tables) |
| Small model compatibility | Full execution path so Ollama models work without pydantic-ai tool-calling |
| pydantic_ai_agent.py patches | trace_id fix, planning via `_direct_llm_json_call`, `_is_direct_json_provider` bypass, SQL sanitizer, two-step summarisation |
| Keyword routing | `_get_routing_hint` / `_get_fallback_sql` / `_is_health_summary` with task state awareness |
| All 6 validation queries | Passing end-to-end with real data |
| Prompt manager wiring | `PROMPTMANAGER_*` env vars injected into kore container, per-model variant support |

---

## 2. How It Works — Architecture

```
User → SELECT answer FROM kore.oscar_ops_agent WHERE question = 'which tasks are currently running?'
         │
         ▼
   PydanticAIAgent._get_completion_stream()
         │
         ├── STEP 1 — Planning  (1 LLM call, always uses json_schema mode)
         │     _direct_llm_json_call(schema=_PLANNING_SCHEMA)
         │     → {"plan": "query TM_History for IN_PROGRESS state", "estimated_steps": 1}
         │
         ├── STEP 2 — Routing  (pure code, zero LLM calls)
         │     _get_routing_hint(question) + _get_fallback_sql(question)
         │
         │     "cpu / memory / disk"       → victoriametrics table hint + example SQL
         │     "task" + "running/active"   → TM_History WHERE state = 'IN_PROGRESS'
         │     "task" + "pending/waiting"  → TM_History WHERE state = 'PENDING'
         │     "task"  (default)           → TM_History WHERE state IN ('FAILURE','FAILED')
         │     "alert / firing"            → AM_AlertHistory hint
         │     "notif / webhook"           → NTF_Notifications_Audit hint
         │     "health summary"            → SENTINEL → skip SQL gen, 3 pre-built queries
         │     (no match)                 → full 14-table data catalog
         │
         ├── STEP 3 — SQL Generation  (1 LLM call)
         │     Ollama / small models (_is_direct_json_provider() == True):
         │       → skip pydantic-ai Agent entirely (saves ~35s of nil-content retry waste)
         │       → _direct_llm_json_call(system=system_prompt, user=routing_hint+question)
         │     Capable models (GPT-4o-mini, Claude, Llama3.3:70b):
         │       → pydantic-ai Agent ReAct loop (native tool-calling, multi-step reasoning)
         │     → AgentResponse{sql_query, type, text, short_description}
         │
         ├── STEP 4 — SQL Execution  (pure code, zero LLM calls)
         │     victoriametrics → prometheus_handler → SQL→PromQL → VictoriaMetrics HTTP API
         │     oscar_db        → mysql_handler      → standard MySQL
         │     On SQL error: rescue_sql = _get_fallback_sql(question) → retry once
         │
         └── STEP 5 — Answer Formatting
               Metric result (has 'value' column):
                 → _format_metric_answer()  ← Python only, no LLM
               oscar_db result (tasks, alerts, notifications):
                 → _direct_llm_json_call(summarise rows → natural language)  ← 1 LLM call
               Health summary sentinel:
                 → Python formats 3 pre-built query results, no LLM SQL gen
         │
         ▼
   answer: "2 tasks are currently running: check_chrony_offset on dc1-web-01 (started 14:32)..."
```

### LLM call count per question type

| Question type | LLM calls | What each call does |
|--------------|-----------|---------------------|
| Metric (CPU/memory/disk) | 1–2 | plan + (SQL gen if routing hint doesn't match exactly) |
| oscar_db (tasks/alerts/notif) | 3 | plan + SQL gen + summarise rows |
| Health summary | 1 | plan only — 3 pre-built queries + Python formatting |

### Key guarantee
The LLM **never makes up numbers**. It only describes what SQL actually returns.
If SQL returns 0 rows, the answer is "No data found."

### Why pydantic-ai is bypassed for Ollama models
pydantic-ai's structured output uses tool-calling. Ollama returns assistant messages with
`content: null` after a tool call. When pydantic-ai retries (up to 5× for planning, 1×
for the main agent), each attempt burns a full inference round-trip (~7–35s) before the
400 error is raised — ~70s total wasted before the real answer starts.

`_direct_llm_json_call` uses `response_format=json_schema` — Ollama handles this correctly
on the first attempt. `_is_direct_json_provider()` detects Ollama by checking
`provider == 'ollama'` or the base URL containing `11434` or `ollama`.

For capable models (GPT-4o-mini, Claude Haiku, Llama3.3:70b) pydantic-ai works natively
and its multi-step ReAct loop is used — see [Section 10](#10-small-model--ollama-execution-path).

---

## 3. The Two Datasources

### `victoriametrics` — Live Metrics

- **Handler:** `prometheus_handler` (custom OSCAR handler)
- **Backend:** VictoriaMetrics at `http://vmdb:8428`
- **Protocol:** SQL → PromQL translation
- **25 tables** covering infra nodes, anomaly detection, and OSCAR operational metrics

**Non-standard SQL syntax required:**
```sql
-- Current snapshot (most common)
WHERE time = 'now'

-- Historical range
WHERE time_start = 'now-6h' AND time_end = 'now' AND step = '5m'

-- Threshold filter (infra tables only — maps to PromQL threshold in PromQL)
AND value = '> 80'     -- string comparison, NOT numeric — e.g. "only hosts where CPU > 80%"
```

**Value filter rules:**
- `value = '> 80'` (string syntax) **works** for named infra tables (`infra_node_cpu_utilization` etc.)
  — the prometheus handler translates this into a PromQL threshold expression
- `WHERE value > 80` (numeric comparison) does **not** work — causes 422
- For `anomaly_zscore`: **never** filter on value in WHERE; return all rows and identify anomalies
  where value > 2.0 in the result set
- The Python `_apply_value_threshold()` in the agent code provides an additional safety layer
  that re-filters results on the Python side after SQL execution

### `oscar_db` — Operational Database

- **Handler:** `mysql_handler`
- **Backend:** MySQL (OSCAR's primary operational DB)
- **Protocol:** Standard MySQL SQL
- **80+ tables** — only 6 are used in the agent (scoped for accuracy)

**Standard MySQL syntax.** Date functions work: `NOW()`, `DATE_SUB()`, `INTERVAL`.

---

## 4. The 14 Curated Tables

These are the tables configured in `oscar_ops_agent`. Kept to 14 (not all 80+) to:
- Keep the data catalog compact (each table adds ~50-100 tokens to every LLM call)
- Reduce routing confusion for smaller models

### victoriametrics (8 tables)

| Table | What it measures | Key columns |
|-------|-----------------|-------------|
| `infra_node_cpu_utilization` | CPU % per server | `meta_hostname`, `value` (0-100%) |
| `infra_node_memory_utilization` | Memory % excl. cache | `meta_hostname`, `value` (0-100%) |
| `infra_node_disk_utilization` | Disk % per mountpoint | `meta_hostname`, `value`, `mountpoint` |
| `anomaly_zscore` | Z-score deviation from 1h baseline | `meta_hostname`, `anomaly_name`, `value` |
| `oscar_alert_active_tasks` | Count of active alert notification tasks | `value` |
| `oscar_alert_queue_depth` | Alert queue depth per queue | `queue_name`, `value` |
| `oscar_task_workers` | Active Celery workers | `value` |
| `oscar_notifier_failed` | Failed notifier count per notifier | `notifier_name`, `value` |

All victoriametrics tables also have: `datacenter`, `environment`, `timestamp`, `instance`, `job`

### oscar_db (6 tables)

| Table | What it contains | Key columns |
|-------|-----------------|-------------|
| `AM_AlertHistory` | All alert events | `alertname`, `status` (firing/resolved), `severity` (critical/warning/info), `summary`, `startsAt`, `endsAt`, `acknowledged`, `occurrence_count`, `ticket_id` |
| `TM_History` | Task run records | `task_id`, `state` (SUCCESS/FAILURE/FAILED/PENDING/IN_PROGRESS), `started`, `succeeded`, `runtime`, `result` (full error JSON) |
| `TM_Tasks` | Task catalog | `id`, `name`, `type` (fabric/ansible/script), `description`, `owner` |
| `NTF_Notifications_Audit` | Notification delivery log | `notifier_name`, `alert_name`, `status` (sent/failed/suppressed), `recipient`, `retry_count`, `status_details` |
| `IM_Servers` | Server inventory | `hostname`, `status`, `is_under_maintenance`, `environment_id` |
| `IM_DataCenters` | Datacenter names | `id`, `name` |

**Key joins:**
```sql
-- Task name (lives in TM_Tasks, not TM_History)
FROM oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id

-- Server datacenter (two-level join)
FROM oscar_db.IM_Servers s
JOIN oscar_db.IM_Environments e ON s.environment_id = e.id
JOIN oscar_db.IM_DataCenters d ON e.datacenter_id = d.id
```

---

## 5. Agent Configuration (current running state)

The agent is stored in Kore's database. Check current config:

```bash
docker exec middleware curl -s "http://kore:47334/api/projects/kore/agents/oscar_ops_agent" \
  | python3 -m json.tool
```

Current settings:
- **Name:** `oscar_ops_agent`
- **Project:** `kore`
- **Model:** `qwen2.5:3b` via Ollama at `http://172.18.0.1:11435/v1`
- **Mode:** `text`
- **Tables:** 14 (see above)
- **Prompt:** Loaded from Prompt Manager — `oscar-ops-agent/qwen2.5-3b` revision 3 (compact small-model prompt)
- **Prompt fallback:** `prompt_template` in agent params (full production prompt) — used if PM unreachable

---

## 6. How to Create or Update the Agent

### Via REST API (recommended — no SQL syntax issues)

```bash
# Create or fully replace the agent
# Paste the full default prompt from Section 6 "System Prompt" into SYSTEM_PROMPT below.
cat << 'PYEOF' | docker exec -i middleware python3
import requests, json

SYSTEM_PROMPT = """You are OSCAR Ops Assistant — an IT operations analyst for the OSCAR AIOps platform.
Answer questions about infrastructure health, alerts, tasks, and notifications by writing
SQL against two datasources and reporting exactly what the data shows.

=== RESPONSE FORMAT ===
Return JSON with these fields:
  sql_query        : the SQL to execute (empty string only for final_text)
  text             : brief description of what this query fetches
  short_description: one-line summary (e.g. "CPU per server right now")
  type             : "final_query"       - run SQL and return result to user
                   | "exploratory_query" - run SQL, use result in next step
                   | "final_text"        - answer without SQL (use sparingly)

=== DATASOURCE ROUTING ===
victoriametrics -> CPU%, memory%, disk%, anomaly z-scores, queue depths, worker counts (live)
oscar_db        -> alert history, task run logs, notification delivery, server inventory (records)

=== VICTORIAMETRICS TABLES ===
All metric values are in the column named "value" — never use cpu, percent, usage, etc.
  infra_node_cpu_utilization    -> meta_hostname, value (CPU %, 0-100)
  infra_node_memory_utilization -> meta_hostname, value (memory %, 0-100, excl. buffers/cache)
  infra_node_disk_utilization   -> meta_hostname, mountpoint, value (disk %, 0-100)
  anomaly_zscore                -> meta_hostname, anomaly_name, value (z-score deviation)
  oscar_alert_active_tasks      -> value (scalar: active notification tasks)
  oscar_alert_queue_depth       -> queue, value (per-queue pending alert count)
  oscar_task_workers            -> value (scalar: active Celery worker count)
  oscar_notifier_failed         -> notifier_name, value (cumulative failed deliveries)

SQL SYNTAX (mandatory):
  Current snapshot : WHERE time = 'now'
  Time range       : WHERE time_start = 'now-6h' AND time_end = 'now' AND step = '5m'
  Threshold filter : AND value = '> 80'  <- STRING syntax, valid for infra tables only

ANOMALY RULE: Never use value filter on anomaly_zscore.
  Return all rows. value > 2.0 = anomalous, value > 3.0 = critical.

=== OSCAR_DB TABLES ===
Standard MySQL syntax. Use NOW(), DATE_SUB(), INTERVAL, CURDATE().

  AM_AlertHistory         - alertname, status (firing/resolved), severity (critical/major/warning/info),
                            summary, startsAt, endsAt, acknowledged, occurrence_count, ticket_id,
                            last_occurrence (timestamp of most recent firing)
  TM_History              - task_id, state (SUCCESS/FAILURE/FAILED/PENDING/IN_PROGRESS/SUBMITTED),
                            started, succeeded, runtime, result
  TM_Tasks                - id, name, type (fabric/ansible/script), description, owner
  NTF_Notifications_Audit - notifier_name, alert_name, status (sent/failed/suppressed),
                            recipient, retry_count, status_details
  IM_Servers              - hostname, status, is_under_maintenance, environment_id
  IM_DataCenters          - id, name

JOIN: FROM oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id

=== RULES ===
1. NEVER invent column names - only use columns listed above.
2. ALWAYS use table aliases; prefix every column in multi-table queries:
   h.state (not state), t.name (not name), s.hostname (not hostname)
3. Firing alerts: ALWAYS add last_occurrence > DATE_SUB(NOW(), INTERVAL 24 HOUR)
4. anomaly_zscore: never filter in WHERE - return all rows.
5. If 0 rows returned - say "No data found"; never guess.
6. Include LIMIT 20; ORDER BY most relevant column first.

=== EXAMPLE QUERIES ===
-- Servers with CPU above 80%
SELECT meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization
WHERE time = 'now' AND value = '> 80' ORDER BY value DESC;
-- Firing alerts (excluding stale)
SELECT alertname, severity, summary, last_occurrence FROM oscar_db.AM_AlertHistory
WHERE status = 'firing' AND last_occurrence > DATE_SUB(NOW(), INTERVAL 24 HOUR)
ORDER BY last_occurrence DESC LIMIT 20;
-- Running tasks
SELECT t.name, h.state, h.started FROM oscar_db.TM_History h
JOIN oscar_db.TM_Tasks t ON h.task_id = t.id WHERE h.state = 'IN_PROGRESS';
-- Failed tasks last hour
SELECT t.name, h.state, h.result, h.started FROM oscar_db.TM_History h
JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
WHERE h.state IN ('FAILURE','FAILED') AND h.started > DATE_SUB(NOW(), INTERVAL 1 HOUR)
ORDER BY h.started DESC LIMIT 20;"""

payload = {
    "agent": {
        "model_name": "llama3.2",
        "provider": "openai",
        "params": {
            "mode": "text",
            "api_key": "ollama",
            "openai_api_base": "http://172.18.0.1:11435/v1"
        },
        "data": {
            "tables": [
                "victoriametrics.infra_node_cpu_utilization",
                "victoriametrics.infra_node_memory_utilization",
                "victoriametrics.infra_node_disk_utilization",
                "victoriametrics.anomaly_zscore",
                "victoriametrics.oscar_alert_active_tasks",
                "victoriametrics.oscar_alert_queue_depth",
                "victoriametrics.oscar_task_workers",
                "victoriametrics.oscar_notifier_failed",
                "oscar_db.AM_AlertHistory",
                "oscar_db.TM_History",
                "oscar_db.TM_Tasks",
                "oscar_db.NTF_Notifications_Audit",
                "oscar_db.IM_Servers",
                "oscar_db.IM_DataCenters"
            ]
        },
        "prompt_template": SYSTEM_PROMPT
    }
}

# PUT to update existing agent
r = requests.put(
    "http://kore:47334/api/projects/kore/agents/oscar_ops_agent",
    json=payload,
    headers={"Content-Type": "application/json"}
)
print(r.status_code, r.text[:200])
PYEOF
```

To create a brand-new agent (first time), use POST:
```bash
# Change PUT to POST
r = requests.post("http://kore:47334/api/projects/kore/agents", ...)
```

### Via Kore SQL (from Kore Editor or mysql client)

```sql
-- Drop and recreate
DROP AGENT IF EXISTS oscar_ops_agent;

CREATE AGENT oscar_ops_agent
USING
  model = {
    'provider':        'ollama',
    'model_name':      'llama3.2',
    'ollama_base_url': 'http://172.18.0.1:11435'
  },
  data = {
    'tables': [
      'victoriametrics.infra_node_cpu_utilization',
      'victoriametrics.infra_node_memory_utilization',
      'victoriametrics.infra_node_disk_utilization',
      'victoriametrics.anomaly_zscore',
      'victoriametrics.oscar_alert_active_tasks',
      'victoriametrics.oscar_alert_queue_depth',
      'victoriametrics.oscar_task_workers',
      'victoriametrics.oscar_notifier_failed',
      'oscar_db.AM_AlertHistory',
      'oscar_db.TM_History',
      'oscar_db.TM_Tasks',
      'oscar_db.NTF_Notifications_Audit',
      'oscar_db.IM_Servers',
      'oscar_db.IM_DataCenters'
    ]
  },
  prompt_template = '<see system prompt below>',
  mode = 'text';
```

### System Prompt

The system prompt is fetched at runtime from **oscar-promptmanager** (if `PROMPTMANAGER_ENABLED=true`)
or falls back to the `prompt_template` stored in the agent's params in MindsDB's database.

See [Section 11](#11-per-model-prompts-via-prompt-manager) for how per-model variants work.

**Default prompt** (for capable models — GPT-4o-mini, Claude, Qwen-72B+)

This is the production-grade prompt. Store it as the `production` environment in oscar-promptmanager,
or paste it into `prompt_template` when creating the agent via the REST API.

```
You are OSCAR Ops Assistant — an IT operations analyst for the OSCAR AIOps platform.
Answer questions about infrastructure health, alerts, tasks, and notifications by writing
SQL against two datasources and reporting exactly what the data shows.

=== RESPONSE FORMAT ===
Return JSON with these fields:
  sql_query        : the SQL to execute (empty string only for final_text)
  text             : brief description of what this query fetches
  short_description: one-line summary (e.g. "CPU per server right now")
  type             : "final_query"      — run SQL and return result to user
                   | "exploratory_query"— run SQL, use result in next step
                   | "final_text"       — answer without SQL (use sparingly)

Use "final_query" for almost every question. Only use "final_text" if genuinely no SQL is needed.

=== DATASOURCE ROUTING ===
victoriametrics → CPU%, memory%, disk%, anomaly z-scores, queue depths, worker counts (live)
oscar_db        → alert history, task run logs, notification delivery, server inventory (records)

=== VICTORIAMETRICS TABLES ===
All metric values are in the column named "value" — never use cpu, percent, usage, etc.
Common columns on every table: value, timestamp, instance, job, datacenter, environment

  infra_node_cpu_utilization    → meta_hostname, value (CPU %, 0-100)
  infra_node_memory_utilization → meta_hostname, value (memory %, 0-100, excl. buffers/cache)
  infra_node_disk_utilization   → meta_hostname, mountpoint, value (disk %, 0-100)
  anomaly_zscore                → meta_hostname, anomaly_name, value (z-score deviation)
  oscar_alert_active_tasks      → value (scalar: count of active notification tasks)
  oscar_alert_queue_depth       → queue, value (per-queue pending alert count)
  oscar_task_workers            → value (scalar: active Celery worker count)
  oscar_notifier_failed         → notifier_name, value (cumulative failed delivery count)

SQL SYNTAX (mandatory — standard SQL does not apply here):
  Current snapshot : WHERE time = 'now'
  Time range       : WHERE time_start = 'now-6h' AND time_end = 'now' AND step = '5m'
  Threshold filter : AND value = '> 80'   ← STRING syntax, valid for infra tables only
  Relative time    : now-15m  now-1h  now-6h  now-24h  now-7d

ANOMALY RULE: Never use "value = '> X'" on anomaly_zscore — return all rows.
  Identify anomalies from the result: value > 2.0 = anomalous, value > 3.0 = critical.

=== OSCAR_DB TABLES ===
Standard MySQL syntax. Date functions: NOW(), DATE_SUB(), INTERVAL, CURDATE().

  AM_AlertHistory         — alertname, status (firing/resolved), severity (critical/major/warning/info),
                            summary, startsAt, endsAt, acknowledged, occurrence_count, ticket_id,
                            last_occurrence (timestamp of most recent firing — key for active alerts)

  TM_History              — task_id, state (SUCCESS/FAILURE/FAILED/PENDING/IN_PROGRESS/SUBMITTED),
                            started, succeeded, runtime, result (full error JSON for failures)

  TM_Tasks                — id, name, type (fabric/ansible/script), description, owner

  NTF_Notifications_Audit — notifier_name, alert_name, status (sent/failed/suppressed),
                            recipient, retry_count, status_details

  IM_Servers              — hostname, status, is_under_maintenance, environment_id

  IM_DataCenters          — id, name

JOIN PATTERNS:
  Task name   : FROM oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
  Datacenter  : FROM oscar_db.IM_Servers s
                JOIN oscar_db.IM_Environments e ON s.environment_id = e.id
                JOIN oscar_db.IM_DataCenters d ON e.datacenter_id = d.id

=== RULES ===
1. NEVER invent column names — only use columns listed above.
2. ALWAYS use table aliases; prefix every column in multi-table queries:
     h.state (not state)   t.name (not name)   s.hostname (not hostname)
   Bare column names cause MySQL "Column 'X' is ambiguous" errors.
3. For "currently firing" / "active" alerts — ALWAYS add:
     AND last_occurrence > DATE_SUB(NOW(), INTERVAL 24 HOUR)
   This excludes stale alerts that fired long ago and were never resolved.
4. anomaly_zscore: never filter in WHERE — return all rows, read z-scores from results.
5. If 0 rows returned — say "No data found"; never guess or fabricate values.
6. Present metric values as percentages (82% not 0.82); network as bytes/s.
7. Always include LIMIT 20 (or less); ORDER BY most relevant column first.

=== EXAMPLE QUERIES ===

-- Servers with CPU above 80% right now
SELECT meta_hostname, value
FROM victoriametrics.infra_node_cpu_utilization
WHERE time = 'now' AND value = '> 80'
ORDER BY value DESC;

-- Currently firing alerts (excluding stale)
SELECT alertname, severity, summary, last_occurrence
FROM oscar_db.AM_AlertHistory
WHERE status = 'firing'
  AND last_occurrence > DATE_SUB(NOW(), INTERVAL 24 HOUR)
ORDER BY last_occurrence DESC LIMIT 20;

-- Currently running tasks (IN_PROGRESS)
SELECT t.name, h.state, h.started
FROM oscar_db.TM_History h
JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
WHERE h.state = 'IN_PROGRESS'
ORDER BY h.started;

-- Failed tasks in the last hour
SELECT t.name, h.state, h.result, h.started
FROM oscar_db.TM_History h
JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
WHERE h.state IN ('FAILURE','FAILED')
  AND h.started > DATE_SUB(NOW(), INTERVAL 1 HOUR)
ORDER BY h.started DESC LIMIT 20;

-- Anomaly detection (no value filter — read z-scores from results)
SELECT meta_hostname, anomaly_name, value
FROM victoriametrics.anomaly_zscore
WHERE time = 'now'
ORDER BY value DESC LIMIT 20;

-- Failed notification deliveries
SELECT notifier_name, alert_name, status_details, retry_count
FROM oscar_db.NTF_Notifications_Audit
WHERE status = 'failed'
ORDER BY id DESC LIMIT 20;

-- CPU trend for one server — last 6 hours
SELECT timestamp, value
FROM victoriametrics.infra_node_cpu_utilization
WHERE meta_hostname = 'dc1-dev-web-01'
  AND time_start = 'now-6h'
  AND time_end = 'now'
  AND step = '5m';
```

---

**Small model prompt** (`qwen2.5-3b`, `llama3.2-3b` variants — tight, rule-focused, ≤400 tokens)

For small models the routing hint (injected into the user message by the code) tells the model
exactly which table to use and provides example SQL. This system prompt provides only the
essential rules to avoid the most common failure modes.

```
You are OSCAR Ops Assistant. Answer IT operations questions by writing SQL and summarising results.

ROUTING: victoriametrics = live metrics (CPU/memory/disk/anomalies)
         oscar_db        = records (alerts/tasks/notifications/inventory)

MANDATORY RULES — follow exactly, no exceptions:
1. Always use table aliases. Prefix EVERY column: h.state NOT state, t.name NOT name.
   Bare column names cause "Column ambiguous" SQL errors.
2. Task join: oscar_db.TM_History h JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
3. Task states: SUCCESS, FAILURE, FAILED, PENDING, IN_PROGRESS, SUBMITTED
4. victoriametrics instant query: WHERE time = 'now'  (always required)
5. victoriametrics threshold (infra tables only): AND value = '> 80'  ← string syntax
6. anomaly_zscore: NEVER filter on value in WHERE — return all rows (z-score > 2.0 = anomalous)
7. Firing alerts: ALWAYS add last_occurrence > DATE_SUB(NOW(), INTERVAL 24 HOUR)
8. Only use columns named in the routing hint — never invent column names.
9. Include LIMIT 20 in every query.

The routing hint above gives you the exact table name and example SQL.
Adapt the WHERE filters to match the question. Keep the JOIN and alias pattern exactly as shown.

Set type to "final_query", put your SQL in sql_query, one-line summary in short_description.
```

> **Note:** `{{question}}` is a legacy template artifact from older prompt manager entries.
> It is NOT substituted — the question arrives as a separate user message.
> Remove it from any new prompt variants you create.

---

## 7. LLM Provider Reference — Swap Without Recompiling

**The `model` block is the ONLY thing that changes between environments.**
Tables, prompt, and all code are identical across all LLMs.
No recompile needed — just update the agent via PUT API.

| Environment | Provider | Model | Notes |
|------------|---------|-------|-------|
| Dev (current) | `ollama` | `llama3.2` | Local, free, needs fallback stack |
| Dev via LLMGW | `openai` | `ollama/llama3.2` | Spend tracked via LLMGW |
| Prod | `openai` | `gpt-4o-mini` | Best cost/quality, no fallback needed |
| Prod | `anthropic` | `claude-3-haiku-20240307` | Fast, cheap, excellent SQL |
| Prod via LLMGW | `openai` | `gpt-4o-mini` | Budget control + model routing |

```python
# Dev — Ollama direct
"params": {"mode": "text", "api_key": "ollama", "openai_api_base": "http://172.18.0.1:11435/v1"},
"model_name": "llama3.2", "provider": "openai"

# NOTE: ollama_base_url MUST include /v1 suffix (OllamaProvider uses AsyncOpenAI internally)
# "ollama_base_url": "http://172.18.0.1:11435/v1"  ← correct
# "ollama_base_url": "http://172.18.0.1:11435"     ← WRONG — causes 404

# Dev — via LLMGW
"params": {"mode": "text", "openai_api_base": "http://llmgw:4000/v1", "api_key": "<master_key>"},
"model_name": "ollama/llama3.2", "provider": "openai"

# Production — OpenAI
"params": {"mode": "text"},
"model_name": "gpt-4o-mini", "provider": "openai"
# api_key set separately via KORE_OPENAI_API_KEY env var or in params

# Production — Anthropic
"params": {"mode": "text"},
"model_name": "claude-3-haiku-20240307", "provider": "anthropic"

# Production — via LLMGW (recommended for prod)
"params": {"mode": "text", "openai_api_base": "http://llmgw:4000/v1", "api_key": "<master_key>"},
"model_name": "gpt-4o-mini", "provider": "openai"
```

### With a better model — what changes
- The pydantic-ai ReAct loop works natively (no fallback triggered)
- Multi-step reasoning: model can do 3 exploratory queries then synthesize
- Better routing: model correctly picks datasource without keyword hints
- Proper NL summaries: model writes fluent paragraphs, not data dumps
- All the fallback code silently steps aside

---

## 8. Validation Queries

All 6 pass with llama3.2 on dev. Run from middleware container or Kore Editor.

```bash
# From middleware container
docker exec middleware curl -s -X POST "http://kore:47334/api/sql/query" \
  -H "Content-Type: application/json" \
  -d '{"query": "SELECT answer FROM kore.oscar_ops_agent WHERE question = '\''which server has the highest CPU right now?'\''"}'
```

```sql
-- Q1: victoriametrics basic snapshot
SELECT answer FROM kore.oscar_ops_agent
WHERE question = 'which server has the highest CPU right now?';
-- Expected: "dc1-dev-app-01 has the highest CPU at 13.7% right now."

-- Q2: oscar_db basic filter with real alert names
SELECT answer FROM kore.oscar_ops_agent
WHERE question = 'which alerts are currently firing?';
-- Expected: Lists HighMemoryUsageWithoutBuffer, HostSwapUsage, HostOutOfDiskSpace, etc.

-- Q3: JOIN test (TM_History → TM_Tasks, hardest single-datasource query)
SELECT answer FROM kore.oscar_ops_agent
WHERE question = 'which tasks failed in the last hour?';
-- Expected: Lists check_chrony_offset failures with socket timeout errors

-- Q4: notification delivery audit
SELECT answer FROM kore.oscar_ops_agent
WHERE question = 'are any notifications failing to deliver?';
-- Expected: Lists Autocaller notifier failures with ticket IDs and SMTP errors

-- Q5: anomaly detection
SELECT answer FROM kore.oscar_ops_agent
WHERE question = 'which servers are showing anomalous behaviour right now?';
-- Expected: dc1-dev-app-01 with network_rx_bytes_rate z-score ~4.9

-- Q6: full cross-datasource health summary
SELECT answer FROM kore.oscar_ops_agent
WHERE question = 'give me a full OSCAR health summary — alerts, tasks, and infrastructure';
-- Expected: Three sections: firing alerts + recent task failures + top CPU servers
```

### Running all 6 from terminal

```bash
for Q in \
  "which server has the highest CPU right now?" \
  "which alerts are currently firing?" \
  "which tasks failed in the last hour?" \
  "are any notifications failing to deliver?" \
  "which servers are showing anomalous behaviour right now?" \
  "give me a full OSCAR health summary — alerts, tasks, and infrastructure"; do
  echo "=== $Q ==="
  docker exec middleware curl -s -X POST "http://kore:47334/api/sql/query" \
    -H "Content-Type: application/json" \
    -d "{\"query\": \"SELECT answer FROM kore.oscar_ops_agent WHERE question = '$Q'\"}" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('data',[['']])[0][0][:300])"
  echo
done
```

---

## 9. Debugging

### Check what SQL the agent generated

```bash
docker logs oscar-kore 2>&1 | grep -E "(Executing|SQL|fallback|WARNING|ERROR)" | tail -30
```

Key log lines to look for:
```
# Healthy (Ollama path — these should NOT appear after the fix)
Planning step failed (...invalid message content type: <nil>...)  ← pydantic-ai nil-content bug (fixed)
pydantic-ai agent failed (Exceeded maximum retries...)            ← pydantic-ai retry waste (fixed)

# Normal operation (these are expected for oscar_db questions)
Direct-JSON provider: bypassing pydantic-ai structured output     ← Ollama fast path active ✅
Fallback SQL execution failed (...)                               ← model SQL was wrong, trying rescue SQL
Rescue SQL also failed (...)                                      ← both attempts failed, returns no-data

# Errors worth investigating
Error running query: Column 'name' is ambiguous                   ← SQL missing table alias prefix
Error running query: ...422...                                    ← tried to filter victoriametrics on value
```

### Manually run a SQL query to verify data exists

```bash
docker exec middleware curl -s -X POST "http://kore:47334/api/sql/query" \
  -H "Content-Type: application/json" \
  -d '{"query": "SELECT alertname, status, severity FROM oscar_db.AM_AlertHistory WHERE status = '\''firing'\'' LIMIT 5"}'
```

### Check agent config

```bash
docker exec middleware curl -s "http://kore:47334/api/projects/kore/agents/oscar_ops_agent" \
  | python3 -m json.tool | head -40
```

### List all agents

```bash
docker exec middleware curl -s "http://kore:47334/api/projects/kore/agents" | python3 -m json.tool
```

### Check datasource connectivity

```bash
docker exec middleware curl -s -X POST "http://kore:47334/api/sql/query" \
  -H "Content-Type: application/json" \
  -d '{"query": "SELECT meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization WHERE time = '\''now'\'' LIMIT 3"}'
```

---

## 10. Small Model / Ollama Execution Path

> For capable models (GPT-4o-mini, Claude Haiku, Llama3.3:70b) pydantic-ai's ReAct loop
> works natively and this section does not apply — the full multi-step reasoning loop runs.

### The root problem with Ollama + pydantic-ai

pydantic-ai enforces structured output via tool-calling. It sends the model a `tools` array
with one entry: `final_result`. The model must respond by "calling" this tool with JSON.

Ollama models return the assistant message as:
```json
{"role": "assistant", "content": null, "tool_calls": [...]}
```

When pydantic-ai retries on validation failure, it sends this message back in the history.
Ollama rejects the null-content message with `400: invalid message content type: <nil>`.
Each retry burns a full inference round-trip. On `qwen2.5:3b`:
- Planning agent (`retries=5`): up to 5 × ~7s = **35s wasted**
- Main agent (`retries=1`): 2 × ~17s = **35s wasted**
- Total: **~70s before the real answer starts**

### The fix: `_is_direct_json_provider()` + `_direct_llm_json_call()`

`_is_direct_json_provider()` detects Ollama providers:
```python
provider == 'ollama'
OR base_url contains '11434' or 'ollama'
```

When detected:
1. **Planning step**: calls `_direct_llm_json_call(schema=_PLANNING_SCHEMA)` directly
2. **Main agent**: raises `RuntimeError` immediately, jumping to the fallback path with zero retry overhead

`_direct_llm_json_call` uses `response_format={"type":"json_schema",...}` which Ollama
handles correctly on the first attempt — no tool-calling, no null-content messages.

### Execution path for Ollama models

```
Planning: _direct_llm_json_call(schema=_PLANNING_SCHEMA)        ~3-5s
Routing:  _get_routing_hint() keyword match                      0s
SQL gen:  _direct_llm_json_call(schema=_AGENT_RESPONSE_SCHEMA)  ~10-20s
SQL exec: MindsDB → MySQL / VictoriaMetrics                      ~1-3s
Summary:  _direct_llm_json_call(schema=_SUMMARY_SCHEMA)         ~5-10s
                                                           Total: ~20-40s
```

### Keyword routing and rescue SQL

| Function | Purpose |
|---------|---------|
| `_get_routing_hint(question)` | Focuses the model on 1-2 tables instead of all 14 |
| `_get_fallback_sql(question)` | Pre-validated SQL used if model's SQL fails |
| `_is_health_summary(question)` | Detects multi-domain overview questions |
| `_HEALTH_SUMMARY_SENTINEL` | Return value that triggers 3-query direct path (no SQL gen LLM call) |

Task routing is state-aware — the routing hint and rescue SQL adapt based on question intent:

| Question keyword | State filter used |
|-----------------|-------------------|
| "running" / "active" / "current" | `state = 'IN_PROGRESS'` |
| "pending" / "waiting" / "queue" | `state = 'PENDING'` |
| "success" / "completed" | `state = 'SUCCESS'` |
| (default) | `state IN ('FAILURE','FAILED')` |

---

## 11. Per-Model Prompts via Prompt Manager

Different model sizes need different prompts. A 3B model cannot handle a 1500-token system
prompt — it loses context and generates wrong SQL. A 70B model benefits from richer examples.

### How prompt resolution works

```
Agent params: prompt_slug  = "oscar-ops-agent"   (default, from KORE_PROMPT_SLUG)
              prompt_env   = "production"          (default, from KORE_PROMPT_ENV)
              model.model_name = "qwen2.5:3b"

_fetch_prompt_from_manager() call order:
  1. Try variant_ref: "oscar-ops-agent/qwen2.5-3b"   ← model-specific variant
     → found: use this prompt  ✅
  2. Try environment_ref: "oscar-ops-agent/production" ← default deployed prompt
     → fallback if variant not found
  3. Use agent.params["prompt_template"]               ← hardcoded fallback in DB
     → fallback if prompt manager unreachable

PROMPTMANAGER_ENABLED=false (default) → skips steps 1+2, uses step 3 directly
```

### Variant naming convention

**Variant name = model name with `:` replaced by `-` and `/` by `-` (lowercase)**

| Model in agent config | Variant name in prompt manager |
|----------------------|-------------------------------|
| `qwen2.5:3b`         | `qwen2.5-3b`                  |
| `qwen2.5:7b`         | `qwen2.5-7b`                  |
| `llama3.2:3b`        | `llama3.2-3b`                 |
| `llama3.3:70b`       | `llama3.3-70b`                |
| `gpt-4o-mini`        | `gpt-4o-mini`                 |
| `gpt-4o`             | `gpt-4o`                      |

The code normalises: `model_name.replace(":", "-").replace("/", "-").lower()`

### Setting up a model-specific prompt in the UI

1. Go to Prompt Manager → `oscar-ops-agent` prompt
2. Click **New Variant**
3. In the **Model** dropdown (shows same list as Chat Playground), select your model
4. The variant name is auto-filled from the model name
5. Write the prompt for that model (see Section 6 for the small model template)
6. Commit the revision — no deployment to an environment needed (variant_ref is used directly)

### When to create a model-specific variant

- Always create one for **small models** (≤7B params): use the tight, rule-focused prompt
- Optional for **capable models**: the `production` environment default works fine
- The `production` environment is the universal fallback — always keep it deployed

### Enabling prompt manager

```bash
# In oscar/overrides.env (already set)
PROMPTMANAGER_ENABLED=true

# Restart kore to pick up
./oscar restart kore
```

> The `PROMPTMANAGER_*` env vars are injected into the kore container via
> `oscar-kore/docker-compose.yml`. If you add a new env var to `overrides.env`,
> you must also add it to the kore docker-compose environment block.

### PM auth fixes required (applied 2026-04-06)

Two bugs prevented kore from fetching prompts from PM. Both are fixed and compiled in.

**Bug 1 — Wrong URL** (`pydantic_ai_agent.py:91`)

The code was POSTing to `/api/v1/prompts/fetch` (405 Method Not Allowed).
The correct endpoint is `/api/v1/fetch`.

**Bug 2 — `kore` not in allowed internal services** (`oscar-promptmanager/src/app/core/auth.py`)

`ALLOWED_INTERNAL_SERVICES` only listed `{"middleware", "taskmanager", "scheduler"}`.
Kore was rejected with `Unknown internal service: kore`.
Fixed by adding `"kore"` to the set.

**Bug 3 — User identity required for internal service calls** (`oscar-promptmanager/src/app/core/auth.py`)

`require_user_identity()` required `X-User-Id` header even for internal services.
Kore has no user identity — it calls PM as a platform service.
Fixed by adding a bypass at the top of `require_user_identity()`:
```python
# Known internal services bypass user identity (they act on behalf of the platform)
internal_service = request.headers.get("X-Internal-Service")
if internal_service and internal_service in ALLOWED_INTERNAL_SERVICES:
    return None
```

**Bug 4 — Prompt visibility must be `public`**

Internal services with no `X-User-Id` can only fetch **public** prompts.
The `oscar-ops-agent` prompt must be set to `public` visibility in the PM UI.
Go to PM UI → `oscar-ops-agent` → Edit → Visibility → Public.

### PM setup checklist (required for PM to serve prompts to kore)

- [x] `PROMPTMANAGER_ENABLED=true` in `overrides.env`
- [x] `oscar-ops-agent` prompt visibility = **public**
- [x] `qwen2.5-3b` variant: plain text compact prompt committed at revision 3
- [x] All three auth bugs fixed and compiled into promptmanager + kore (2026-04-06)
- [ ] `production` environment: a revision deployed (serves as fallback for all other models — not yet done)

**Current status (2026-04-06):** Kore is successfully loading prompts from PM:
```
[KORE] Loaded system prompt from promptmanager variant_ref=oscar-ops-agent/qwen2.5-3b revision=3
```

### Variant prompt format in the UI

When creating/editing a variant in the PM UI, the messages field must be a JSON array:
```json
[
  {
    "role": "system",
    "content": "Your prompt text here..."
  }
]
```
The `content` field must be **plain text** — do not nest another JSON structure inside it.

---

## 12. What Changes Per Environment vs What Never Changes

### NEVER changes (permanent Kore config)
- The 14 table list in `data.tables`
- The prompt template routing rules
- The `mode = 'text'` setting
- The pydantic_ai_agent.py code changes

### Changes per environment (model block only)
- `provider` — ollama / openai / anthropic / groq
- `model_name` — llama3.2 / gpt-4o-mini / claude-3-haiku / etc.
- `api_key` — provider API key
- `openai_api_base` — custom endpoint URL (LLMGW or Ollama)

Update via REST API — no recompile needed:
```bash
docker exec middleware curl -s -X PUT "http://kore:47334/api/projects/kore/agents/oscar_ops_agent" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": {
      "model_name": "gpt-4o-mini",
      "provider": "openai",
      "params": {"mode": "text", "api_key": "sk-..."}
    }
  }'
```

---

## 13. Phase Roadmap

| Phase | Status | What |
|-------|--------|------|
| 1 — Metrics pipeline | ✅ Done | 25 VictoriaMetrics tables, recording rules, anomaly detection pipeline |
| 2 — NL→SQL Agent | ✅ Done | `oscar_ops_agent` with 14 tables, qwen2.5:3b, all 6 queries passing, PM wired |
| 2.1 — Prompt Manager wiring | ✅ Done | PM auth fixed, kore fetches `qwen2.5-3b` variant revision 3 on every query |
| 3 — Vector KB (RAG) | Next | Runbooks, SOPs, past incidents searchable by the agent |
| 4 — Graph DB | Future | Neo4j service dependency map, blast radius queries |

### Phase 3 preview — adding a Knowledge Base

When Phase 3 is built, add `knowledge_bases` alongside `tables`:
```sql
data = {
  'tables': ['victoriametrics.infra_node_cpu_utilization', ...],
  'knowledge_bases': ['oscar_runbooks_kb']
}
```
The agent then pulls from live SQL data AND the vector KB in the same question.
Example: "which servers are at risk? (check runbook for disk thresholds)"

---

## Appendix: Key Files

| File | Location | Purpose |
|------|----------|---------|
| `pydantic_ai_agent.py` | `mindsdb/interfaces/agents/` | Main agent execution, fallback stack |
| `text_sql.py` | `mindsdb/interfaces/agents/modes/` | AgentResponse schema for mode='text' |
| `base.py` | `mindsdb/interfaces/agents/modes/` | ResponseType enum (FINAL_TEXT, FINAL_QUERY, EXPLORATORY) |
| `prometheus_handler.py` | `mindsdb/integrations/handlers/prometheus_handler/` | SQL→PromQL translation |
| `PYDANTIC_AI_FALLBACK.md` | `mindsdb/integrations/handlers/prometheus_handler/` | Deep-dive: fallback architecture |
| `AI_SQL_CHEATSHEET.md` | `mindsdb/integrations/handlers/prometheus_handler/` | VictoriaMetrics SQL quick reference |
| `OSCAR_DB_CHEATSHEET.md` | `mindsdb/integrations/handlers/prometheus_handler/` | oscar_db SQL quick reference |
| `AGENT_GUIDE.md` | `mindsdb/integrations/handlers/prometheus_handler/` | This document |
