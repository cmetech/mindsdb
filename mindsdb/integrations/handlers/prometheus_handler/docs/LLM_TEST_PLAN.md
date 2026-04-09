# OSCAR Agent — LLM Test Plan & Benchmark

> **How to use this document**
> Paste the entire **"System Context"** section into the LLM chat first as a system/context message.
> Then run each test question one by one.
> Score the response using the rubric. Fill in the results table at the bottom.
> Compare across models to pick the best fit for production.

---

## System Context (paste this into every LLM you test)

```
You are an AI operations assistant embedded in OSCAR — an AIOps platform for IT infrastructure.
You have access to two live data sources:

1. victoriametrics — real-time metrics from the infrastructure
   Tables:
   - infra_node_cpu_utilization    : columns: meta_hostname, value, datacenter, environment. value = CPU % (0-100). Filter: WHERE time = 'now'
   - infra_node_memory_utilization : columns: meta_hostname, value, datacenter, environment. value = memory % (0-100). Filter: WHERE time = 'now'
   - infra_node_disk_utilization   : columns: meta_hostname, value, datacenter, environment. value = disk % (0-100). Filter: WHERE time = 'now'
   - anomaly_zscore                : columns: meta_hostname, anomaly_name, value. value = z-score (>2.0 = anomalous). Filter: WHERE time = 'now'. DO NOT add WHERE value > X.

2. oscar_db — operational MySQL database
   Tables:
   - AM_AlertHistory : alertname, status ('firing'/'resolved'), severity ('critical'/'warning'/'info'), summary, startsAt, endsAt, acknowledged, occurrence_count
   - TM_History      : id, task_id, state ('SUCCESS'/'FAILURE'/'FAILED'/'PENDING'/'IN_PROGRESS'), started, succeeded, runtime, result
   - TM_Tasks        : id, name, type, description  [JOIN TM_History h ON h.task_id = TM_Tasks.id]
   - NTF_Notifications_Audit : notifier_name, alert_name, status ('sent'/'failed'/'suppressed'), recipient, retry_count, status_details

IMPORTANT RULES:
- Always query real data — never make up hostnames, alert names, or values
- If a query returns 0 rows, say so clearly — do not invent results
- For victoriametrics: you CANNOT filter WHERE value > X in SQL (the handler rejects it). Fetch all rows and filter in your response logic.
- For threshold questions ("above 50%", "below 20%"): fetch all rows, then only report hosts that meet the threshold
- Present CPU/memory/disk values as percentages (e.g. "73.4%" not "73.4")
- Keep answers concise and factual
```

---

## Test Questions

Each question has:
- **Category** — domain being tested
- **Question** — exact text to send
- **What to look for** — quality criteria for scoring
- **Red flags** — signs of a bad answer

---

### T01 — CPU: Basic Top-N

**Question:**
```
Which server has the highest CPU usage right now?
```

**What to look for:**
- Names a specific hostname (e.g. ROG-Strix, dc1-dev-web-01)
- Gives a numeric CPU percentage
- Uses past-tense "at time of query" language

**Red flags:**
- Invents a hostname not in the data
- Says "I cannot access real-time data"
- Returns a markdown table header with no data rows
- Returns a percentage that doesn't match the question (hallucinated value)

**Scoring:**
| Criterion | Points |
|-----------|--------|
| Names a real hostname | 2 |
| Gives numeric % value | 2 |
| Correct grammar / readable | 1 |
| **Total** | **5** |

---

### T02 — CPU: Threshold Above

**Question:**
```
Show me all servers with CPU usage above 50%
```

**Context:** In the test environment ROG-Strix is typically around 7–11% CPU. No server exceeds 50%.

**What to look for:**
- Returns "No servers found with CPU above 50%" (or equivalent)
- Does NOT invent a server that meets the threshold

**Red flags:**
- Returns a server with CPU at 10% claiming it's above 50%
- Partial sentence like "Here are the servers with CPU above 50%:" followed by nothing
- Says it can't check

**Scoring:**
| Criterion | Points |
|-----------|--------|
| Correctly returns no results | 3 |
| Clear explanation ("none exceed 50%") | 1 |
| Does not hallucinate a qualifying server | 1 |
| **Total** | **5** |

---

### T03 — CPU: Threshold Below

**Question:**
```
Show me all servers with CPU usage below 5%
```

**Context:** Most servers should be below 5% (dc1-dev-web-01, dc1-dev-app-01 typically at 0.1–0.2%).

**What to look for:**
- Lists servers that are genuinely below 5%
- Excludes ROG-Strix if it's above 5% at query time

**Red flags:**
- Lists ROG-Strix (which is typically 7–11%) as below 5%
- Returns empty when there clearly are servers below 5%

**Scoring:**
| Criterion | Points |
|-----------|--------|
| Lists at least one real server below 5% | 2 |
| Does not include servers above threshold | 2 |
| Values shown as percentages | 1 |
| **Total** | **5** |

---

### T04 — Memory: High Usage

**Question:**
```
Which servers have memory usage above 50%?
```

**Context:** ROG-Strix is typically around 75% memory. Should appear in results.

**What to look for:**
- ROG-Strix named with ~75% memory
- No other servers listed unless they genuinely exceed 50%

**Red flags:**
- Returns datacenter servers (0.1–0.2%) as above 50%
- Completely empty result when ROG-Strix is clearly high

**Scoring:**
| Criterion | Points |
|-----------|--------|
| ROG-Strix appears in results | 2 |
| Value is approximately correct (60–90%) | 2 |
| Threshold filtering is correct | 1 |
| **Total** | **5** |

---

### T05 — Disk: Overview

**Question:**
```
What is the disk usage across all servers?
```

**What to look for:**
- Lists all servers with their disk % values
- ROG-Strix should show ~80% disk
- dc1 servers typically 10–15%

**Red flags:**
- Only returns one server
- Invents disk values not from the data
- Returns memory values instead of disk

**Scoring:**
| Criterion | Points |
|-----------|--------|
| Lists multiple servers | 2 |
| Values are plausible (not 0% for everything) | 2 |
| Correctly identifies which server has highest disk | 1 |
| **Total** | **5** |

---

### T06 — Alerts: Currently Firing

**Question:**
```
Which alerts are currently firing?
```

**What to look for:**
- Lists real alert names (e.g. HighCPU, DiskWarning, etc.)
- Includes severity if available
- Uses status = 'firing' filter

**Red flags:**
- Returns resolved alerts as firing
- Invents alert names
- Returns 0 results when there are active alerts

**Scoring:**
| Criterion | Points |
|-----------|--------|
| Queries correct table (AM_AlertHistory) | 1 |
| Returns firing alerts with real names | 2 |
| Includes severity/summary | 1 |
| Correct count / doesn't truncate without saying so | 1 |
| **Total** | **5** |

---

### T07 — Alerts: Specific Severity

**Question:**
```
Are there any critical alerts firing right now?
```

**What to look for:**
- Filters on both status='firing' AND severity='critical'
- If no critical alerts: says so clearly
- Does not mix up severity values

**Red flags:**
- Returns warning alerts as critical
- Returns all alerts regardless of severity
- Hallucinated alert names

**Scoring:**
| Criterion | Points |
|-----------|--------|
| Filters severity='critical' correctly | 2 |
| Clear answer (yes with list, or "none currently") | 2 |
| Does not confuse severity levels | 1 |
| **Total** | **5** |

---

### T08 — Tasks: Recent Failures

**Question:**
```
Which tasks failed in the last hour and what was the error?
```

**What to look for:**
- JOINs TM_History with TM_Tasks to get task name
- Returns actual task names (not UUIDs)
- Shows the error/result from TM_History.result
- Filters by started > last hour

**Red flags:**
- Returns task IDs instead of task names (missed the JOIN)
- Returns all task history without time filter
- Makes up error messages

**Scoring:**
| Criterion | Points |
|-----------|--------|
| Uses correct JOIN pattern | 1 |
| Task names shown (not raw IDs) | 2 |
| Error details included | 1 |
| Time filter applied | 1 |
| **Total** | **5** |

---

### T09 — Notifications: Delivery Failures

**Question:**
```
Are any notifications failing to deliver? Show me details.
```

**What to look for:**
- Queries NTF_Notifications_Audit with status='failed'
- Returns notifier name, alert name, and error details
- Shows retry count if relevant

**Red flags:**
- Returns 'suppressed' as 'failed'
- Invents notifier names
- Returns empty when failures exist

**Scoring:**
| Criterion | Points |
|-----------|--------|
| Correct table and status filter | 2 |
| Notifier name + error detail shown | 2 |
| Retry count mentioned | 1 |
| **Total** | **5** |

---

### T10 — Anomaly Detection

**Question:**
```
Which servers are showing anomalous behaviour right now?
```

**What to look for:**
- Queries anomaly_zscore table
- Identifies z-score > 2.0 as anomalous
- Returns hostname + anomaly name + z-score value
- Does NOT add WHERE value > 2 (prometheus handler rejects it)

**Red flags:**
- Adds WHERE value > 2.0 causing query failure
- Reports all servers as anomalous without z-score threshold
- Invents anomaly names

**Scoring:**
| Criterion | Points |
|-----------|--------|
| Fetches anomaly_zscore table | 1 |
| Identifies threshold correctly (z > 2.0) | 2 |
| Returns hostnames + anomaly names | 1 |
| Does not add value filter in SQL | 1 |
| **Total** | **5** |

---

### T11 — Health Summary (Multi-Domain)

**Question:**
```
Give me a full OSCAR health summary — alerts, tasks, and infrastructure
```

**What to look for:**
- Covers all 3 domains: alerts (firing count + top alerts), tasks (recent failures), infra (top CPU servers)
- Each section has real data
- Structured / readable output

**Red flags:**
- Only covers 1 or 2 domains
- Generic "all systems normal" without querying data
- Mixes domains incorrectly

**Scoring:**
| Criterion | Points |
|-----------|--------|
| Covers alerts section with real data | 2 |
| Covers tasks section with real data | 2 |
| Covers infra section with real data | 2 |
| Structured and readable | 1 |
| No hallucinated data | 1 |
| **Total** | **8** |

---

### T12 — Edge Case: No Data

**Question:**
```
Show me all servers with CPU above 99%
```

**What to look for:**
- Correctly returns "no servers found" or equivalent
- Does NOT invent a server at 99%+

**Red flags:**
- Returns any server as above 99%
- Partial sentence with no data
- Error message instead of clean "no results" response

**Scoring:**
| Criterion | Points |
|-----------|--------|
| Clean "no servers found" response | 3 |
| Does not hallucinate | 2 |
| **Total** | **5** |

---

### T13 — Edge Case: Ambiguous Question

**Question:**
```
How is the system doing?
```

**What to look for:**
- Either asks for clarification OR runs a health summary
- Does not make up system state
- Produces something actionable

**Red flags:**
- Returns "the system is healthy" with no data
- Fails to respond at all
- Confuses what "system" refers to

**Scoring:**
| Criterion | Points |
|-----------|--------|
| Produces useful response (clarification or summary) | 3 |
| Does not hallucinate system state | 2 |
| **Total** | **5** |

---

## Scoring Summary Table

Fill this in after running all 13 tests per model.

| Test | Max | llama3.2 | GPT-4o-mini | GPT-4o | Claude Haiku | Claude Sonnet | Llama3.3-70B |
|------|-----|----------|-------------|--------|--------------|---------------|--------------|
| T01 CPU Top-N | 5 | | | | | | |
| T02 CPU Above 50% | 5 | | | | | | |
| T03 CPU Below 5% | 5 | | | | | | |
| T04 Memory Above 50% | 5 | | | | | | |
| T05 Disk Overview | 5 | | | | | | |
| T06 Firing Alerts | 5 | | | | | | |
| T07 Critical Alerts | 5 | | | | | | |
| T08 Task Failures | 5 | | | | | | |
| T09 Notification Failures | 5 | | | | | | |
| T10 Anomaly Detection | 5 | | | | | | |
| T11 Health Summary | 8 | | | | | | |
| T12 No Data Edge Case | 5 | | | | | | |
| T13 Ambiguous Question | 5 | | | | | | |
| **TOTAL** | **68** | | | | | | |

---

## Grading Scale

| Score | Grade | Recommendation |
|-------|-------|----------------|
| 60–68 | A — Excellent | Production ready, no special handling needed |
| 50–59 | B — Good | Production ready with minor prompt tuning |
| 38–49 | C — Acceptable | Use with Python fallback layer (like llama3.2 path) |
| 25–37 | D — Poor | Needs heavy prompt engineering, not recommended |
| < 25  | F — Fail | Do not use for this use case |

---

## How the OSCAR Agent Handles Each Model

When you test via the KORE editor (`SELECT answer FROM kore.oscar_ops_agent WHERE question = '...'`):

```
┌─────────────────────────────────────────────────────┐
│ Question comes in                                   │
│                                                     │
│ ① Health summary?  → 3 pre-built SQLs + format     │
│ ② CPU/Memory/Disk? → pre-validated SQL + Python     │  ← no LLM for SQL/format
│    _apply_value_threshold() filters by threshold    │
│    _format_metric_answer() builds the answer        │
│                                                     │
│ ③ Alert/Task/Notif? → LLM generates SQL            │  ← LLM involved here
│    If SQL fails → rescue SQL from _EXAMPLE_SQL      │
│    LLM summarises oscar_db rows into prose          │
│                                                     │
│ ④ Unknown → LLM with full 14-table catalog          │
└─────────────────────────────────────────────────────┘
```

**For a stronger model (GPT-4o-mini, Claude Haiku):**
The pydantic-ai tool-calling path works natively — the entire fallback stack is bypassed.
Only the `model = {}` block in CREATE AGENT needs to change:

```sql
-- Swap only this block:
model = {
  'provider':   'openai',
  'model_name': 'gpt-4o-mini',
  'api_key':    'sk-...'
}
```

Everything else (tables, prompt_template, mode) stays identical.

---

## Notes Column (fill in per model)

Use this to capture qualitative observations:

| Model | Key Observations |
|-------|-----------------|
| llama3.2 | |
| GPT-4o-mini | |
| GPT-4o | |
| Claude Haiku | |
| Claude Sonnet | |
| Llama3.3-70B | |
