# pydantic_ai_agent.py — llama3.2 Fallback Architecture

> **Audience:** Engineers maintaining or debugging the Kore agent code.
> **File:** `mindsdb/interfaces/agents/pydantic_ai_agent.py`
> **Purpose:** Why the fallback exists, every component, every code change made.

---

## Background: Why a Fallback Was Needed

MindsDB's agent runs on **pydantic-ai**, a structured-output LLM framework. pydantic-ai works by sending the LLM a `tools` array with one entry called `final_result`. The model must respond by "calling" that tool with JSON matching the `AgentResponse` Pydantic schema.

```
pydantic-ai sends:
{
  "tools": [{
    "name": "final_result",
    "parameters": {
      "sql_query": {"type": "string"},
      "text": {"type": "string"},
      "type": {"type": "string", "enum": ["final_query", "exploratory_query", "final_text"]},
      "short_description": {"type": "string"}
    }
  }]
}

GPT-4o-mini responds:
{"role": "assistant", "tool_calls": [{"name": "final_result", "arguments": {...}}]}  ✅

llama3.2 (3B) responds:
{"role": "assistant", "content": "I'll query the database..."}  ❌
or calls a different tool entirely:
{"tool_calls": [{"name": "query", "arguments": {"db": "victoriametrics"}}]}  ❌
```

pydantic-ai validates the response and retries. After N retries: `Exceeded maximum retries (N) for output validation`. The agent crashes with no answer.

**Root cause:** llama3.2 is a 3B parameter model. It can generate valid SQL and answer questions, but it doesn't reliably follow OpenAI tool-calling protocol. This is a fundamental capability limitation, not a prompt issue.

**Solution:** Three-layer fallback that bypasses pydantic-ai's tool-calling protocol while keeping the same execution flow.

---

## Overview: The 3-Layer Fallback Stack

```
Request comes in
      │
      ▼
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1: Planning fallback                                     │
│                                                                 │
│  planning_agent.run_sync(planning_prompt)                       │
│    ├── SUCCESS → use plan (estimated_steps, plan text)          │
│    └── FAILURE → hardcoded PlanResponse(plan="Query directly")  │
└─────────────────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────────────────────────────┐
│  Layer 2: Main agent fallback (in ReAct loop)                   │
│                                                                 │
│  agent.run_sync(current_prompt)                                 │
│    ├── SUCCESS → use output (AgentResponse)                     │
│    └── FAILURE → keyword routing decision:                      │
│         ├── health summary → run 3 pre-built queries directly   │
│         ├── keyword match  → _direct_llm_json_call (focused)    │
│         └── no match       → _direct_llm_json_call (full catalog)│
└─────────────────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────────────────────────────┐
│  Layer 3: SQL execution + summarisation                         │
│                                                                 │
│  For mode='text' when output.type == final_query/exploratory:   │
│    1. Strip non-SQL prefix from output.sql_query                │
│    2. execute(clean_sql)                                        │
│         ├── SUCCESS → _direct_llm_json_call to summarise        │
│         └── FAILURE → try rescue SQL from _get_fallback_sql()  │
│              ├── SUCCESS → _direct_llm_json_call to summarise   │
│              └── FAILURE → "No data found or query failed."     │
└─────────────────────────────────────────────────────────────────┘
      │
      ▼
   output.type = "final_text"
   output.text = "dc1-dev-app-01 has the highest CPU at 13.7%..."
      │
      ▼
   yield {"type": "data", "text": output.text}
      │
      ▼
   get_completion adds trace_id column to DataFrame
      │
      ▼
   SELECT answer → "dc1-dev-app-01 has the highest CPU at 13.7%..."
```

---

## Layer 1: Planning Fallback

**Location:** ~line 509

**What pydantic-ai does normally:**
Runs the planning agent first to estimate how many steps the question needs (`PlanResponse` with `plan: str` and `estimated_steps: int`).

**The problem:**
llama3.2 fails the same tool-calling protocol in the planning phase. The neutral planning system prompt (`"You are a data analyst. Create a brief query plan."`) triggers retries=5 to exhaust, then raises.

**The fix:**
```python
try:
    plan_result = planning_agent.run_sync(planning_prompt_text)
    plan = plan_result.output
except Exception as plan_err:
    logger.warning(f"Planning step failed ({plan_err}), using default plan.")
    plan = PlanResponse(
        plan="Query the relevant tables directly to answer the question in one step.",
        estimated_steps=1,
    )
```

The hardcoded fallback plan is always safe — it tells the main loop to attempt a direct 1-step query. The main loop handles multi-step naturally via `exploratory_query` iterations regardless of what the plan says.

**Why retries=5 on the planning agent:**
```python
planning_agent = Agent(
    self.model_instance,
    system_prompt="You are a data analyst...",
    output_type=PlanResponse,
    retries=5,
)
```
More retries = more chances for llama3.2 to get lucky and return valid JSON. In practice it never does after 1 try, but the cost is only a few extra seconds.

---

## Layer 2: Main Agent Fallback + Keyword Routing

**Location:** ~line 580

### The fallback trigger

```python
try:
    result = agent.run_sync(
        current_prompt,
        message_history=message_history if message_history else None,
    )
    output = result.output
except Exception as agent_err:
    logger.warning(f"pydantic-ai agent failed ({agent_err}), falling back to direct JSON call")
    # ... routing + direct call
```

### `_direct_llm_json_call` — the core of the fallback

```python
def _direct_llm_json_call(llm_params: dict, system_prompt: str, user_prompt: str, schema: dict = None) -> dict:
```

Uses `openai.OpenAI` (synchronous) to call the LLM with `response_format=json_schema`. This is Ollama's native structured output — bypasses pydantic-ai tool-calling entirely.

```python
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
```

**Why this works when pydantic-ai doesn't:**
`response_format=json_schema` tells Ollama to use its own constrained-decoding mechanism (beam search guided by the JSON schema). The model output is guaranteed to be valid JSON matching the schema. It never needs to "call a tool" — it just generates the right JSON directly.

**Two schemas in use:**

`_AGENT_RESPONSE_SCHEMA` — for the main agent call:
```python
{
    "type": "object",
    "properties": {
        "sql_query":         {"type": "string"},
        "text":              {"type": "string"},
        "short_description": {"type": "string"},
        "type":              {"type": "string", "enum": ["final_query", "exploratory_query", "final_text"]},
    },
    "required": ["sql_query", "text", "short_description", "type"],
}
```

`_SUMMARY_SCHEMA` — for the summarisation call:
```python
{
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Concise natural language answer"},
    },
    "required": ["answer"],
}
```

### Keyword routing — the routing decision

**Problem:** With 14 tables, the data catalog is 800-1200 tokens. llama3.2 (3B params) reads ALL table schemas and confuses `IM_Servers.hostname` with `infra_node_cpu_utilization.meta_hostname`. Result: CPU questions get routed to the server inventory table.

**Solution:** Detect question keywords in Python, bypass the full catalog, give the model ONLY the relevant table info.

```python
routing_hint = _get_routing_hint(original_question)
if routing_hint == _HEALTH_SUMMARY_SENTINEL:
    # Run 3 pre-built queries directly, skip LLM SQL generation
elif routing_hint:
    # Focused mode: only routing hint (1-2 tables), no 14-table catalog
    focused_user = f"{routing_hint}{prev}\nQuestion: {original_question}"
    raw = _direct_llm_json_call(self.llm_params, self.system_prompt, focused_user)
    output = AgentResponse(**raw)
else:
    # No match: pass full catalog (for open-ended questions)
    focused_user = f"{data_catalog}{prev}\n\nQuestion: {original_question}"
    raw = _direct_llm_json_call(self.llm_params, self.system_prompt, focused_user)
    output = AgentResponse(**raw)
```

### `_get_routing_hint(question)` — what it returns

For each question type, returns a string that tells the model:
1. Exact table name to use
2. Exact column names
3. A ready-to-use example SQL

```python
# CPU question example
if any(k in q for k in ["cpu", "processor", "utilization"]):
    return (
        "You must query the table named: victoriametrics.infra_node_cpu_utilization\n"
        "Exact column names: meta_hostname, value, datacenter, environment\n"
        "value = CPU percentage (0-100). Required filter: WHERE time = 'now'\n"
        "Use this SQL (adapt filters as needed):\n"
        "SELECT meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization "
        "WHERE time = 'now' ORDER BY value DESC LIMIT 5\n\n"
    )
```

The routing hint replaces the full data catalog. The model sees ~6 lines instead of ~200 lines.

**Keyword mapping:**

| Keywords | Routes to |
|---------|-----------|
| cpu, processor, utilization | `victoriametrics.infra_node_cpu_utilization` |
| memory, mem, ram | `victoriametrics.infra_node_memory_utilization` |
| disk, storage, inode | `victoriametrics.infra_node_disk_utilization` |
| anomal, zscore, z-score | `victoriametrics.anomaly_zscore` |
| alert, firing, incident, acknowledged | `oscar_db.AM_AlertHistory` |
| task, celery, job run | `oscar_db.TM_History` + `oscar_db.TM_Tasks` |
| notif, notifier, email, webhook, delivery | `oscar_db.NTF_Notifications_Audit` |
| health summary, full summary, overview, OR 2+ domains combined | `_HEALTH_SUMMARY_SENTINEL` |

### `_is_health_summary(question)` — multi-domain detection

```python
def _is_health_summary(question: str) -> bool:
    q = question.lower()
    multi_domain = sum([
        any(k in q for k in ["alert", "firing"]),
        any(k in q for k in ["task", "failed"]),
        any(k in q for k in ["cpu", "memory", "infrastructure", "infra", "server"]),
    ])
    return multi_domain >= 2 or any(k in q for k in ["health summary", "full summary", "overview", ...])
```

When True, routing returns `_HEALTH_SUMMARY_SENTINEL` (a special string constant, not a hint).

### Health summary direct path

Runs 3 pre-built queries directly in Python — no LLM SQL generation:

```python
for label, key in [
    ("Firing alerts",       "health_alerts"),
    ("Recent task failures","health_tasks"),
    ("Top CPU servers",     "health_cpu"),
]:
    df = self.sql_toolkit.execute_sql(_EXAMPLE_SQL[key], ...)
    # format as "Row N: col=val, col=val, ..."
    summary_sections.append(f"{label} ({len(rows)} rows):\n" + rows)

combined = "\n\n".join(summary_sections)
output = AgentResponse(sql_query="", text=combined, ..., type="final_text")
```

Why return raw data instead of LLM summary? llama3.2 fails to summarise 3 sections of structured data in one call — returns empty. For production models (GPT-4o-mini etc.), the entire fallback is bypassed and the pydantic-ai ReAct loop handles health summary via multi-step exploration naturally.

---

## Layer 3: SQL Execution + Summarisation

**Location:** ~line 677

This layer handles the `mode='text'` requirement: users want natural language answers, not raw SQL result tables. The `select_targets=["answer"]` from the SQL query (`SELECT answer FROM ...`) means only the `answer` column is expected.

### The problem without this layer

If model returns `type="final_query"`:
- MindsDB executes the SQL → gets a DataFrame with real columns (`meta_hostname`, `value`, etc.)
- `select_targets=["answer"]` tries to select the "answer" column from that DataFrame
- No "answer" column exists → column is added as `None` → user gets `null`

### The two-step summarisation

```python
if self.agent_mode == "text" and output.type in ("final_query", "exploratory_query") and output.sql_query:

    # Step 1: SQL sanitizer — strip any non-SQL prefix from model output
    _sql_match = re.search(r'\b(SELECT|WITH|INSERT|UPDATE|DELETE)\b', output.sql_query, re.IGNORECASE)
    clean_sql = output.sql_query[_sql_match.start():].strip() if _sql_match else output.sql_query

    # Step 2: Execute the SQL
    sql_result = self.sql_toolkit.execute_sql(clean_sql, escape_identifiers=True)

    # Step 3: Format results as plain "Row N: col=val" lines (NOT markdown — llama3.2 echoes markdown headers)
    result_lines = []
    for i, row in enumerate(sql_result.head(20).to_dict(orient="records"), 1):
        fields = ", ".join(f"{k}={v}" for k, v in row.items() if v is not None and str(v) != "nan")
        result_lines.append(f"Row {i}: {fields}")

    # Step 4: Ask model to summarise the rows
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
        schema=_SUMMARY_SCHEMA,  # just {"answer": string}
    )
    output = AgentResponse(sql_query=..., text=summary_raw.get("answer", ""), type="final_text")
```

**Why plain "Row N: col=val" instead of markdown table:**
llama3.2 sees a markdown table (`| alertname | severity | ... |`) and echoes the header row as its answer. Plain key=value per line is much easier for small models to process.

### SQL sanitizer

The routing hint includes "Use this SQL (adapt filters as needed):\nSELECT ...". llama3.2 puts the entire routing hint text (including the "Use this SQL:" prefix) into the `sql_query` field. MindsDB's parser sees the prefix and fails.

```python
_sql_match = re.search(r'\b(SELECT|WITH|INSERT|UPDATE|DELETE)\b', output.sql_query, re.IGNORECASE)
clean_sql = output.sql_query[_sql_match.start():].strip() if _sql_match else output.sql_query
```

This finds the first keyword that starts a valid SQL statement and truncates everything before it.

### Rescue SQL

If `execute(clean_sql)` fails (model generated wrong table name, wrong alias, etc.):

```python
except Exception as exec_err:
    logger.warning(f"Fallback SQL execution failed ({exec_err}), trying example SQL")
    rescue_sql = _get_fallback_sql(original_question)
    if rescue_sql and rescue_sql != clean_sql:
        sql_result = self.sql_toolkit.execute_sql(rescue_sql, ...)
        # ... same summarisation flow
```

`_get_fallback_sql` returns pre-validated, tested SQL for each question type — the exact SQL from `_EXAMPLE_SQL` dict. These are known to work against the live datasources.

---

## The `trace_id` Fix

**Location:** ~line 433, in `get_completion()`

**The bug (before fix):**
`agents.py:379` does:
```python
trace_id = completion.iloc[-1]["trace_id"]  # KeyError
```
The DataFrame returned by `get_completion` when the agent returns `type="data"` with `"content"` (raw SQL results) didn't have a `trace_id` column. The HTTP request handler crashed.

**The fix:**
```python
# After select_targets filtering, before return data
if data is not None and isinstance(data, pd.DataFrame) and TRACE_ID_COLUMN not in data.columns:
    data[TRACE_ID_COLUMN] = self.langfuse_client_wrapper.get_trace_id()
return data
```

Matches what `_create_error_response` already does for error cases (line 438-439).

---

## The `original_question` Variable

**Location:** ~line 539

**The bug (before fix):**
The main loop overwrites `current_prompt` with `base_prompt` at the start of each iteration. The fallback was using `self._current_prompt` (a class attribute, always `None`) as the question for the summarisation call.

Result: the summarisation prompt contained `None` as the question → model confused → garbage SQL output with Unicode emoji.

**The fix:**
```python
# Save original question before current_prompt gets overwritten in the loop
original_question = current_prompt
```

Saved before the loop. Used in all fallback paths as the authoritative question text.

---

## Data Flow: Complete End-to-End

```
1. HTTP request:
   POST /api/sql/query {"query": "SELECT answer FROM kore.oscar_ops_agent WHERE question='which server has highest CPU?'"}

2. MindsDB executor:
   → looks up oscar_ops_agent → finds PydanticAIAgent
   → calls get_completion(messages=[{role:user, content:"which server has highest CPU?"}], args={})
   → sets self.select_targets = ["answer"]

3. get_completion:
   → iterates over _get_completion_stream()
   → collects last "data" message
   → adds trace_id column
   → applies select_targets filter (extracts "answer" column)
   → returns DataFrame: [{"answer": "dc1-dev-app-01 has the highest CPU at 13.7%", "trace_id": "..."}]

4. _get_completion_stream:
   → builds data catalog (schema + 5 sample rows for all 14 tables)
   → planning: fallback → PlanResponse(plan="Query directly", estimated_steps=1)
   → saves original_question = "which server has highest CPU?"
   → ReAct loop iteration 1:
        → agent.run_sync() → FAILS (llama3.2 tool-calling)
        → routing_hint = "You must query: victoriametrics.infra_node_cpu_utilization\n..."
        → focused_user = f"{routing_hint}\nQuestion: which server has highest CPU?"
        → _direct_llm_json_call() → {"sql_query": "SELECT meta_hostname, value FROM victoriametrics.infra_node_cpu_utilization WHERE time='now' ORDER BY value DESC LIMIT 5", "type": "final_query", ...}
        → output = AgentResponse(sql_query=..., type="final_query")
        → mode='text' two-step:
             → clean_sql = strip prefix → "SELECT meta_hostname, value FROM ..."
             → sql_result = execute(clean_sql) → DataFrame: [{meta_hostname: dc1-dev-app-01, value: 13.7}, ...]
             → result_plain = "Row 1: meta_hostname=dc1-dev-app-01, value=13.7\nRow 2: ..."
             → summary_raw = _direct_llm_json_call("Using ONLY the data above...") → {"answer": "dc1-dev-app-01 has the highest CPU..."}
             → output = AgentResponse(type="final_text", text="dc1-dev-app-01 has the highest CPU...")
        → output.type == FINAL_TEXT
        → yield {"type": "data", "text": "dc1-dev-app-01 has the highest CPU..."}
        → yield {"type": "end"}
        → return

5. get_completion receives {"type": "data", "text": "dc1-dev-app-01..."}
   → data = pd.DataFrame([{"answer": "dc1-dev-app-01 has the highest CPU..."}])
   → add trace_id
   → return DataFrame

6. HTTP response:
   {"type": "table", "data": [["dc1-dev-app-01 has the highest CPU at 13.7% right now."]], "column_names": ["answer"]}
```

---

## What Works vs What Doesn't With llama3.2

| Capability | llama3.2 | GPT-4o-mini |
|-----------|---------|------------|
| Single-table queries (victoriametrics) | ✅ via fallback | ✅ natively |
| Single-table queries (oscar_db) | ✅ via fallback + rescue SQL | ✅ natively |
| JOIN queries (TM_History → TM_Tasks) | ✅ via rescue SQL | ✅ natively |
| Correct datasource routing (14 tables) | ✅ via keyword routing | ✅ natively |
| Multi-step exploration | ❌ always uses fallback after 1 try | ✅ 3-5 exploratory steps |
| Health summary (3 sources) | ✅ via pre-built queries | ✅ properly synthesised NL |
| Proper NL summaries | ~ (short, sometimes misses context) | ✅ fluent paragraphs |
| anomaly zscore filter (value > 2) | ❌ not supported by prometheus handler | ❌ same limitation |
| Context window with 14 tables | ✅ with routing bypass | ✅ no bypass needed |

---

## Adding a New Question Type to the Routing

If you add a new datasource or table and want to ensure llama3.2 can answer questions about it:

1. Add the table to `_EXAMPLE_SQL` dict:
```python
"_EXAMPLE_SQL["my_new_table"] = "SELECT col1, col2 FROM my_ds.my_table WHERE status = 'active' LIMIT 10"
```

2. Add keyword detection to `_get_routing_hint()`:
```python
if any(k in q for k in ["my_keyword", "another_keyword"]):
    return (
        "You must query the table named: my_ds.my_table\n"
        "Exact column names: col1, col2, col3\n"
        "Use this SQL (adapt filters as needed):\n"
        "SELECT col1, col2 FROM my_ds.my_table WHERE status = 'active' LIMIT 10\n\n"
    )
```

3. Add the same keywords to `_get_fallback_sql()`:
```python
if any(k in q for k in ["my_keyword", "another_keyword"]):
    return _EXAMPLE_SQL["my_new_table"]
```

4. Test manually:
```bash
docker exec middleware curl -s -X POST "http://kore:47334/api/sql/query" \
  -H "Content-Type: application/json" \
  -d '{"query": "SELECT answer FROM kore.oscar_ops_agent WHERE question = '\''my new question?'\''"}'
```

No recompile needed unless you changed pydantic_ai_agent.py.
If you changed pydantic_ai_agent.py: `./oscar compile kore`

---

## Known Limitations

### `WHERE value > 2` on victoriametrics tables

The prometheus handler translates SQL WHERE to PromQL. Numeric value filters (`AND value > 2`) generate invalid PromQL. **Workaround:** fetch all rows, identify anomalies by the returned values in the summary.

Current anomaly_zscore routing hint explicitly warns: "Do NOT add any AND value filter".

### max_tokens=1000 in _direct_llm_json_call

Set to 1000 to keep responses fast and avoid context overflow. For questions that return many rows, the summary answer may be cut short. Increase if needed — it's a single constant in `_direct_llm_json_call`.

### llama3.2 table name mangling

llama3.2 occasionally corrupts table names (`AM_AlertHistory` → `AMアルERT_history`). This is caught by the rescue SQL path. With a production model, this never happens.

### Health summary returns raw data, not NL

For the full health summary, the answer is raw structured data (Row 1: alertname=...) rather than a prose summary. This is intentional — llama3.2 fails the summarisation step for large combined inputs. Production models produce proper NL summaries via the pydantic-ai ReAct path.
