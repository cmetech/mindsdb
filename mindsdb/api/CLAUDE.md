# MindsDB API Layer

## Overview
Multi-protocol access layer providing HTTP REST, MySQL wire protocol, MCP (Model Context Protocol), and A2A (Agent-to-Agent) interfaces. All protocols share the same query execution engine.

## Architecture
```
┌─────────────────────────────────────────────────────────────┐
│                    Starlette (ASGI)                         │
├──────────┬──────────┬───────────┬───────────┬──────────────┤
│ HTTP/REST│  MySQL   │    MCP    │    A2A    │   LiteLLM    │
│ (Flask)  │ (Socket) │(Starlette)│(Starlette)│   (Async)    │
└────┬─────┴────┬─────┴─────┬─────┴─────┬─────┴──────┬───────┘
     │          │           │           │            │
     └──────────┴───────────┴───────────┴────────────┘
                            │
                  ┌─────────▼─────────┐
                  │  FakeMysqlProxy   │  ← Protocol abstraction
                  └─────────┬─────────┘
                            │
                  ┌─────────▼─────────┐
                  │  CommandExecutor  │  ← SQL dispatch
                  └─────────┬─────────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
     ┌────────▼───┐ ┌───────▼─────┐ ┌─────▼──────┐
     │QueryPlanner│ │  SQLQuery   │ │  DataHub   │
     │ (AST→Steps)│ │(Step Exec)  │ │(Data Access)│
     └────────────┘ └─────────────┘ └────────────┘
```

## Directory Structure

| Directory | Purpose | Entry Point |
|-----------|---------|-------------|
| `http/` | REST API (Flask-RESTX) | `start.py` |
| `mysql/` | MySQL wire protocol | `start.py` |
| `executor/` | Query execution engine | `command_executor.py` |
| `mcp/` | Claude MCP server | SSE-based |
| `a2a/` | Agent-to-Agent protocol | Starlette app |
| `litellm/` | LLM server bridge | `start.py` |
| `common/` | Shared middleware/auth | PAT authentication |

## Key Abstractions

### FakeMysqlProxy - Protocol Unification
All protocols use this single interface:
```python
proxy = FakeMysqlProxy()
result: SQLAnswer = proxy.process_query("SELECT * FROM db.table")
# Returns: SQLAnswer(resp_type, result_set, status)
```

### Query Execution Flow
```
SQL String → parse_sql() → AST
    → QueryPlanner.plan() → QueryPlan(steps=[...])
    → SQLQuery.execute_query() → step handlers
    → DataHub → Integration handlers
    → ResultSet(columns, rows)
```

### Step-Based Execution
Query planner generates ordered steps:
- `FetchDataframeStep` - Retrieve data from integrations
- `ProjectStep` - Column selection
- `JoinStep`, `UnionStep` - Multi-source operations
- `ApplyPredictorStep` - ML model inference
- `InsertStep`, `UpdateStep`, `DeleteStep` - Mutations

## HTTP Namespaces (`http/namespaces/`)

| Namespace | Endpoints | Purpose |
|-----------|-----------|---------|
| `sql.py` | `/api/sql/query` | SQL execution |
| `models.py` | `/api/models/*` | ML model CRUD |
| `databases.py` | `/api/databases/*` | Integration management |
| `projects.py` | `/api/projects/*` | Project namespace |
| `agents.py` | `/api/agents/*` | AI agents |
| `skills.py` | `/api/skills/*` | Agent skills |
| `chatbots.py` | `/api/chatbots/*` | Conversational interfaces |
| `jobs.py` | `/api/jobs/*` | Scheduled queries |
| `auth.py` | `/api/auth/*` | Authentication |

## Authentication

| Protocol | Method |
|----------|--------|
| HTTP | Session cookies OR PAT tokens (Bearer) |
| MySQL | Salt-based scramble handshake |
| MCP/A2A | PAT tokens |

PAT tokens: `pat_` prefix + 32 random bytes, HMAC-SHA256 fingerprinting

## Session Controller
Created per-request, provides access to all business logic:
```python
session = SessionController(api_type='http')
session.database_controller   # Integration management
session.model_controller      # ML models
session.agents_controller     # AI agents
session.datahub              # Unified data access
```

## DataHub - Data Access Layer
Polymorphic interface for different data sources:
- `IntegrationDataNode` - External database handlers
- `ProjectDataNode` - MindsDB artifacts (models, views)
- `InformationSchemaDataNode` - Metadata tables
- `SystemTablesDataNode` - System information

## Ports (Default)
- HTTP: 47334
- MySQL: 47335
- LiteLLM: 8000
