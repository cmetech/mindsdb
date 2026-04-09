# OSCAR Kore — oscar_db SQL Cheat Sheet

> **Purpose:** Reference for manual queries + NL→SQL agent training against the OSCAR MySQL database.
> All queries run against the `oscar_db` datasource in Kore (MindsDB → MySQL).
>
> **When to use `oscar_db` vs `victoriametrics`:**
>
> | Question type | Use |
> |---|---|
> | Current metric values, trends, rates | `victoriametrics` |
> | Anomaly z-scores, adaptive bands | `victoriametrics` |
> | Alerts that are firing / resolved — with full context | `oscar_db` |
> | Which tasks ran, succeeded, or failed — with details | `oscar_db` |
> | Notification delivery — who got what, did it fail | `oscar_db` |
> | Server inventory — hostnames, datacenters, maintenance | `oscar_db` |
> | LLM token usage and spend | `oscar_db` |
> | "Right now" operational gauges (queue depth, workers) | `victoriametrics` |

---

## Quick Reference — Key Tables

| Table | Domain | Key columns | Rows (approx) |
|-------|--------|-------------|---------------|
| `AM_AlertHistory` | Alerts | alertname, status, severity, startsAt, acknowledged, ticket_id | ~443 |
| `AM_AlertHistoryLabel` | Alert labels | AlertHistoryID, Label, Value | many |
| `TM_History` | Task runs | task_id, name, state, started, succeeded, runtime, routing_key | ~550k |
| `TM_Tasks` | Task catalog | name, type, status, owner, description | ~267 |
| `TM_StageHistory` | Task stage details | task_history_id, task_stage, stage_name | — |
| `NTF_Notifications_Audit` | Notification delivery | alert_name, notifier_name, status, recipient, retry_count | ~33k |
| `NTF_Notifiers` | Notifier configs | name, type, status | — |
| `IM_Servers` | Inventory | hostname, status, is_under_maintenance | ~297 |
| `IM_DataCenters` | Datacenters | name | — |
| `IM_Environments` | Environments | name, datacenter_id | — |
| `LLM_Spend_Logs` | LLM usage | username, model, total_tokens, spend, response_time_ms | ~17 |
| `LLM_Budgets` | LLM budget limits | scope, max_limit, soft_limit, duration | — |

---

## TM_History — State Values

| State | Meaning |
|-------|---------|
| `PENDING` | Queued, not yet started |
| `IN_PROGRESS` | Currently executing |
| `SUBMITTED` | Submitted to worker |
| `SUCCESS` | Completed successfully |
| `FAILURE` | Failed with error |
| `FAILED` | Celery-level failure (worker crash, timeout) |

---

## ── SECTION 1: Alert History ────────────────────────────────────────────────

### All currently firing alerts
```sql
SELECT alertname, severity, startsAt, occurrence_count, ticket_id, summary
FROM oscar_db.AM_AlertHistory
WHERE status = 'firing'
ORDER BY severity, startsAt DESC;
```

### Firing alerts — only critical and major
```sql
SELECT alertname, severity, startsAt, occurrence_count, ticket_id, summary
FROM oscar_db.AM_AlertHistory
WHERE status = 'firing'
  AND severity IN ('critical', 'major')
ORDER BY severity, startsAt;
```

### Unacknowledged firing alerts (need attention)
```sql
SELECT alertname, severity, startsAt, occurrence_count, ticket_id, summary
FROM oscar_db.AM_AlertHistory
WHERE status = 'firing'
  AND acknowledged = 0
ORDER BY severity, startsAt;
```

### Alerts with tickets (ticketing integration active)
```sql
SELECT alertname, severity, status, ticket_id, startsAt, summary
FROM oscar_db.AM_AlertHistory
WHERE ticket_id IS NOT NULL
ORDER BY startsAt DESC
LIMIT 50;
```

### Alert by fingerprint (full details)
```sql
SELECT fingerprint, alertname, status, severity, startsAt, endsAt,
       acknowledged, acknowledger, occurrence_count, ticket_id,
       summary, description, resolution_notes
FROM oscar_db.AM_AlertHistory
WHERE fingerprint = 'a730a0f4d4f3';
```

### Alerts that fired today
```sql
SELECT alertname, severity, status, startsAt, endsAt, occurrence_count, ticket_id
FROM oscar_db.AM_AlertHistory
WHERE DATE(startsAt) = CURDATE()
ORDER BY startsAt DESC;
```

### Alerts in the last 24 hours
```sql
SELECT alertname, severity, status, startsAt, endsAt, occurrence_count
FROM oscar_db.AM_AlertHistory
WHERE startsAt >= NOW() - INTERVAL 24 HOUR
ORDER BY startsAt DESC;
```

### Recently resolved alerts
```sql
SELECT alertname, severity, startsAt, endsAt, resolved_at, resolved_by, resolution_notes
FROM oscar_db.AM_AlertHistory
WHERE status = 'resolved'
ORDER BY resolved_at DESC
LIMIT 20;
```

### Alert count breakdown by severity
```sql
SELECT severity, status, COUNT(*) AS count
FROM oscar_db.AM_AlertHistory
WHERE status = 'firing'
GROUP BY severity, status
ORDER BY FIELD(severity, 'critical', 'major', 'minor', 'warning', 'info');
```

### Most recurring alerts (top 10 by occurrence_count)
```sql
SELECT alertname, severity, occurrence_count, status, startsAt, ticket_id
FROM oscar_db.AM_AlertHistory
ORDER BY occurrence_count DESC
LIMIT 10;
```

### Alerts by label — look up a specific host's alerts
```sql
SELECT h.alertname, h.severity, h.status, h.startsAt, h.summary
FROM oscar_db.AM_AlertHistory h
JOIN oscar_db.AM_AlertHistoryLabel l ON h.ID = l.AlertHistoryID
WHERE l.Label = 'instance'
  AND l.Value LIKE '%192.168.29.195%'
ORDER BY h.startsAt DESC;
```

### What labels does a specific alert have?
```sql
SELECT Label, Value
FROM oscar_db.AM_AlertHistoryLabel
WHERE AlertHistoryID = 42
ORDER BY Label;
```

---

## ── SECTION 2: Task Execution History ──────────────────────────────────────

> `TM_History.task_id` links to `TM_Tasks.id` for the task name.
> `TM_History.name` is the internal Celery queue name (e.g. `tm.tasks.common`).
> `routing_key`: `tm_tasks` = automation tasks, `tm_notifier` = notification delivery.

### Recent task runs (last 20)
```sql
SELECT h.id, t.name AS task_name, t.type, h.state, h.started, h.succeeded, h.runtime, h.routing_key
FROM oscar_db.TM_History h
JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
ORDER BY h.created_at DESC
LIMIT 20;
```

### Recent FAILURES with details
```sql
SELECT h.id, t.name AS task_name, t.type, h.state, h.started, h.runtime, h.result
FROM oscar_db.TM_History h
JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
WHERE h.state IN ('FAILURE', 'FAILED')
ORDER BY h.created_at DESC
LIMIT 20;
```

### Failures in the last 1 hour
```sql
SELECT h.id, t.name AS task_name, t.type, h.state, h.started, h.result
FROM oscar_db.TM_History h
JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
WHERE h.state IN ('FAILURE', 'FAILED')
  AND h.started >= NOW() - INTERVAL 1 HOUR
ORDER BY h.started DESC;
```

### Task run counts by state (overall health)
```sql
SELECT state, COUNT(*) AS count
FROM oscar_db.TM_History
GROUP BY state
ORDER BY count DESC;
```

### Task run counts today
```sql
SELECT state, COUNT(*) AS count
FROM oscar_db.TM_History
WHERE DATE(created_at) = CURDATE()
GROUP BY state;
```

### Slowest task runs (top 10 by runtime)
```sql
SELECT h.id, t.name AS task_name, t.type, h.state, h.runtime, h.started
FROM oscar_db.TM_History h
JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
WHERE h.state = 'SUCCESS'
  AND h.runtime IS NOT NULL
ORDER BY h.runtime DESC
LIMIT 10;
```

### Currently running tasks (IN_PROGRESS)
```sql
SELECT h.id, t.name AS task_name, t.type, h.started, h.routing_key
FROM oscar_db.TM_History h
JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
WHERE h.state = 'IN_PROGRESS'
ORDER BY h.started;
```

### Tasks queued but not started (PENDING backlog)
```sql
SELECT h.routing_key, COUNT(*) AS pending_count
FROM oscar_db.TM_History h
WHERE h.state = 'PENDING'
GROUP BY h.routing_key
ORDER BY pending_count DESC;
```

### History for a specific task by name
```sql
SELECT h.id, h.state, h.started, h.succeeded, h.runtime, h.result
FROM oscar_db.TM_History h
JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
WHERE t.name = 'custom:check_chrony_offset:titan'
ORDER BY h.created_at DESC
LIMIT 20;
```

### Failure rate per task (last 7 days)
```sql
SELECT t.name AS task_name,
       COUNT(*) AS total,
       SUM(CASE WHEN h.state IN ('FAILURE','FAILED') THEN 1 ELSE 0 END) AS failures
FROM oscar_db.TM_History h
JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
WHERE h.created_at >= NOW() - INTERVAL 7 DAY
  AND h.state IN ('SUCCESS','FAILURE','FAILED')
GROUP BY t.name
ORDER BY failures DESC
LIMIT 20;
```

---

## ── SECTION 3: Task Catalog ─────────────────────────────────────────────────

### All enabled tasks
```sql
SELECT name, type, owner, description
FROM oscar_db.TM_Tasks
WHERE status = 'enabled'
ORDER BY type, name;
```

### Tasks by type
```sql
SELECT type, COUNT(*) AS count
FROM oscar_db.TM_Tasks
GROUP BY type;
```

### Find a task by keyword
```sql
SELECT name, type, status, owner, description
FROM oscar_db.TM_Tasks
WHERE name LIKE '%chrony%'
   OR description LIKE '%chrony%';
```

### All fabric tasks
```sql
SELECT name, status, owner, description
FROM oscar_db.TM_Tasks
WHERE type = 'fabric'
ORDER BY name;
```

### All ansible tasks
```sql
SELECT name, status, owner, description
FROM oscar_db.TM_Tasks
WHERE type = 'ansible'
ORDER BY name;
```

### Tasks owned by a specific person
```sql
SELECT name, type, status, description
FROM oscar_db.TM_Tasks
WHERE owner = 'Corey Ellis'
ORDER BY type, name;
```

---

## ── SECTION 4: Notification Delivery ───────────────────────────────────────

> `NTF_Notifications_Audit` is the primary delivery audit trail.
> `status` values: `sent`, `failed`, `suppressed`
> `suppressed` = deduplication TTL hit or maintenance window — not a failure.

### All notifications sent today
```sql
SELECT alert_name, severity, notifier_name, notifier_type, recipient,
       status, sent_at, environment, datacenter
FROM oscar_db.NTF_Notifications_Audit
WHERE DATE(created_at) = CURDATE()
ORDER BY created_at DESC;
```

### Failed notifications (delivery errors)
```sql
SELECT alert_name, severity, notifier_name, notifier_type, recipient,
       status_details, failed_at, retry_count, response_code
FROM oscar_db.NTF_Notifications_Audit
WHERE status = 'failed'
ORDER BY failed_at DESC
LIMIT 20;
```

### Suppressed notifications (dedup / maintenance)
```sql
SELECT alert_name, severity, notifier_name, status, suppression_reason,
       datacenter, environment, created_at
FROM oscar_db.NTF_Notifications_Audit
WHERE status = 'suppressed'
ORDER BY created_at DESC
LIMIT 20;
```

### Notification delivery breakdown by status
```sql
SELECT status, COUNT(*) AS count
FROM oscar_db.NTF_Notifications_Audit
WHERE created_at >= NOW() - INTERVAL 24 HOUR
GROUP BY status;
```

### Notifications for a specific alert name
```sql
SELECT notifier_name, notifier_type, recipient, status, sent_at, failed_at, retry_count
FROM oscar_db.NTF_Notifications_Audit
WHERE alert_name = 'HostOutOfDiskSpace'
ORDER BY created_at DESC
LIMIT 20;
```

### Notifications per notifier — volume today
```sql
SELECT notifier_name, notifier_type, status, COUNT(*) AS count
FROM oscar_db.NTF_Notifications_Audit
WHERE DATE(created_at) = CURDATE()
GROUP BY notifier_name, notifier_type, status
ORDER BY count DESC;
```

### Notifications with retries (delivery problems)
```sql
SELECT alert_name, notifier_name, recipient, retry_count, status, status_details
FROM oscar_db.NTF_Notifications_Audit
WHERE retry_count > 0
ORDER BY retry_count DESC, created_at DESC
LIMIT 20;
```

### Configured notifiers (what's set up?)
```sql
SELECT name, type, status
FROM oscar_db.NTF_Notifiers
ORDER BY type, name;
```

---

## ── SECTION 5: Inventory ────────────────────────────────────────────────────

### All active servers
```sql
SELECT hostname, status, is_under_maintenance
FROM oscar_db.IM_Servers
WHERE status = 'active'
ORDER BY hostname;
```

### Servers under maintenance
```sql
SELECT hostname, status
FROM oscar_db.IM_Servers
WHERE is_under_maintenance = 1;
```

### Server count by status
```sql
SELECT status, COUNT(*) AS count
FROM oscar_db.IM_Servers
GROUP BY status;
```

### All datacenters
```sql
SELECT name FROM oscar_db.IM_DataCenters ORDER BY name;
```

### All environments
```sql
SELECT e.name AS environment, d.name AS datacenter
FROM oscar_db.IM_Environments e
JOIN oscar_db.IM_DataCenters d ON e.datacenter_id = d.id
ORDER BY d.name, e.name;
```

### Servers in a specific datacenter (via join)
```sql
SELECT s.hostname, s.status, s.is_under_maintenance, e.name AS environment
FROM oscar_db.IM_Servers s
JOIN oscar_db.IM_Environments e ON s.environment_id = e.id
JOIN oscar_db.IM_DataCenters d ON e.datacenter_id = d.id
WHERE d.name = 'titan'
ORDER BY s.hostname;
```

### Find a server by hostname
```sql
SELECT s.hostname, s.status, s.is_under_maintenance,
       e.name AS environment
FROM oscar_db.IM_Servers s
JOIN oscar_db.IM_Environments e ON s.environment_id = e.id
WHERE s.hostname LIKE '%kvm%'
ORDER BY s.hostname;
```

---

## ── SECTION 6: LLM Usage & Spend ───────────────────────────────────────────

### All LLM usage — most recent
```sql
SELECT username, model, total_tokens, spend, response_time_ms, created_at
FROM oscar_db.LLM_Spend_Logs
ORDER BY created_at DESC
LIMIT 20;
```

### Total tokens and spend per user
```sql
SELECT username, SUM(total_tokens) AS total_tokens, SUM(spend) AS total_spend
FROM oscar_db.LLM_Spend_Logs
GROUP BY username
ORDER BY total_tokens DESC;
```

### Token usage by model
```sql
SELECT model, COUNT(*) AS requests, SUM(total_tokens) AS total_tokens, AVG(response_time_ms) AS avg_ms
FROM oscar_db.LLM_Spend_Logs
GROUP BY model
ORDER BY total_tokens DESC;
```

### LLM usage this week
```sql
SELECT username, model, total_tokens, spend, response_time_ms, created_at
FROM oscar_db.LLM_Spend_Logs
WHERE created_at >= NOW() - INTERVAL 7 DAY
ORDER BY created_at DESC;
```

---

## ── SECTION 7: NL → SQL Patterns ───────────────────────────────────────────

| User says | SQL pattern / table |
|-----------|---------------------|
| "What alerts are firing right now?" | `AM_AlertHistory WHERE status = 'firing'` |
| "Show me critical alerts" | `AM_AlertHistory WHERE status = 'firing' AND severity = 'critical'` |
| "Which alerts haven't been acknowledged?" | `AM_AlertHistory WHERE status = 'firing' AND acknowledged = 0` |
| "What happened to alert X?" | `AM_AlertHistory WHERE fingerprint = '...'` |
| "Which alerts have tickets?" | `AM_AlertHistory WHERE ticket_id IS NOT NULL` |
| "Show me alerts from the last hour" | `AM_AlertHistory WHERE startsAt >= NOW() - INTERVAL 1 HOUR` |
| "Which tasks failed recently?" | `TM_History JOIN TM_Tasks WHERE state IN ('FAILURE','FAILED') ORDER BY created_at DESC` |
| "What was the error on task X?" | `TM_History JOIN TM_Tasks WHERE t.name = 'X' AND state = 'FAILURE' — check result column` |
| "What tasks are running right now?" | `TM_History WHERE state = 'IN_PROGRESS'` |
| "How many tasks are pending?" | `TM_History WHERE state = 'PENDING' GROUP BY routing_key` |
| "Which fabric tasks exist?" | `TM_Tasks WHERE type = 'fabric'` |
| "Find tasks related to chrony" | `TM_Tasks WHERE name LIKE '%chrony%'` |
| "Did any notifications fail today?" | `NTF_Notifications_Audit WHERE status = 'failed' AND DATE(created_at) = CURDATE()` |
| "Why was a notification suppressed?" | `NTF_Notifications_Audit WHERE status = 'suppressed' — check status_details` |
| "Which notifiers are configured?" | `NTF_Notifiers WHERE status = 'active'` |
| "How many servers are under maintenance?" | `IM_Servers WHERE is_under_maintenance = 1` |
| "Which servers are in datacenter titan?" | `IM_Servers JOIN IM_Environments JOIN IM_DataCenters WHERE d.name = 'titan'` |
| "How much have we spent on LLM this week?" | `LLM_Spend_Logs WHERE created_at >= NOW() - INTERVAL 7 DAY — SUM(spend)` |
| "Which models are being used?" | `LLM_Spend_Logs GROUP BY model` |

---

## ── SECTION 8: Cross-Datasource Patterns ───────────────────────────────────

Some questions require combining `oscar_db` + `victoriametrics` in separate queries:

### "Server X has high CPU — is it also failing tasks?"
```sql
-- Step 1: metrics
SELECT value FROM victoriametrics.infra_node_cpu_utilization
WHERE meta_hostname = 'ti-p1-kvm-01' AND time = 'now';

-- Step 2: DB
SELECT h.state, h.started, h.result, t.name AS task_name
FROM oscar_db.TM_History h
JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
WHERE h.state IN ('FAILURE','FAILED')
  AND h.started >= NOW() - INTERVAL 1 HOUR;
```

### "Are there alerts firing AND tasks failing at the same time?"
```sql
-- Firing alerts
SELECT alertname, severity, startsAt, summary
FROM oscar_db.AM_AlertHistory WHERE status = 'firing';

-- Task failures in same window
SELECT t.name, h.state, h.started
FROM oscar_db.TM_History h
JOIN oscar_db.TM_Tasks t ON h.task_id = t.id
WHERE h.state IN ('FAILURE','FAILED')
  AND h.started >= NOW() - INTERVAL 1 HOUR;
```

### "Is there a notification backlog? Queue depth + delivery audit"
```sql
-- Metric: queue backlog right now
SELECT queue, value FROM victoriametrics.oscar_alert_queue_depth WHERE time = 'now';

-- DB: notifications suppressed vs sent today
SELECT status, COUNT(*) FROM oscar_db.NTF_Notifications_Audit
WHERE DATE(created_at) = CURDATE() GROUP BY status;
```
