# MindsDB Utilities

## Overview
Shared infrastructure: configuration, logging, context management, caching, distributed task queue, and observability.

## Core Utilities

### Configuration (`config.py`)
Singleton with multi-source hierarchy (highest to lowest priority):
1. Command-line arguments
2. Environment variables (`MINDSDB_*`)
3. `config.auto.json` (GUI-editable)
4. `config.json` (user-provided)
5. Default values

```python
from mindsdb.utilities.config import config
value = config['storage_dir']
config.update({'key': 'value'})  # Persists to config.auto.json
```

**Key Environment Variables**:
- `MINDSDB_STORAGE_DIR` - Storage root
- `MINDSDB_DB_CON` - Database connection string
- `MINDSDB_LOG_LEVEL` - Logging level
- `MINDSDB_DEFAULT_PROJECT` - Default project name

### Logging (`log.py`)
```python
from mindsdb.utilities import log
logger = log.getLogger(__name__)
```

**Features**:
- `ColorFormatter` - Color-coded console output
- `LogSanitizer` - Auto-masks sensitive data (passwords, tokens, API keys)
- File rotation support

### Context (`context.py`)
Thread-safe context using `ContextVar`:
```python
from mindsdb.utilities.context import context
context.user_id = user_id
context.company_id = company_id  # Multi-tenancy
context.session_id = session_id

# Serialize for passing to threads
data = context.dump()
context.load(data)
```

**Fields**: `user_id`, `company_id`, `session_id`, `task_id`, `user_class`, `profiling`

### Caching (`cache.py`)
```python
from mindsdb.utilities.cache import get_cache, dataframe_checksum
cache = get_cache('predict_cache')
key = dataframe_checksum(df)
cache.set(key, result)
result = cache.get(key)
```

**Backends**: `FileCache` (local), `RedisCache` (distributed), `NoCache`

## ML Task Queue (`ml_task_queue/`)

Redis-backed distributed task processing for ML operations:

```
┌─────────────────┐         ┌─────────────────┐
│ MindsDB Server  │         │ MindsDB Server  │
│   (Producer)    │         │   (Producer)    │
└────────┬────────┘         └────────┬────────┘
         │                           │
         └───────────┬───────────────┘
                     │
              ┌──────▼──────┐
              │Redis Stream │
              │ 'ml-tasks'  │
              └──────┬──────┘
                     │
         ┌───────────┴───────────┐
         │                       │
   ┌─────▼─────┐          ┌──────▼────┐
   │ Consumer  │          │ Consumer  │
   │    #1     │          │    #2     │
   └───────────┘          └───────────┘
```

**Task Types**: `LEARN`, `PREDICT`, `FINETUNE`, `DESCRIBE`, `CREATE_ENGINE`

```python
from mindsdb.utilities.ml_task_queue import MLTaskProducer, ML_TASK_TYPE
producer = MLTaskProducer()
task = producer.apply_async(ML_TASK_TYPE.PREDICT, model_id, payload, dataframe)
result = task.result()  # Blocks until complete
```

**Consumer startup**: `python -m mindsdb --ml_task_queue_consumer`

## Hooks System (`hooks/`)

Plugin system for extending functionality:
- `after_predict` - Post-prediction hook
- `after_api_query` - Post-query hook
- `before_openai_query` / `after_openai_query` - LLM hooks
- `send_profiling_results` - Profiling data submission

## OpenTelemetry (`otel/`)

Observability integration (disabled by default on "local" environment):

```bash
OTEL_EXPORTER_TYPE=otlp          # or "console"
OTEL_OTLP_ENDPOINT=http://localhost:4317
OTEL_SERVICE_NAME=mindsdb
OTEL_TRACE_SAMPLE_RATE=1.0
OTEL_SDK_FORCE_RUN=true          # Force enable
```

**Components**: Tracing, Metrics, Logging (each independently toggleable)

## Profiling (`profiler/`)

Hierarchical execution timing:
```python
from mindsdb.utilities.profiler import profiler
profiler.enable()
profiler.start_node('operation')
# ... code ...
profiler.stop_current_node()
```

**Metrics**: Wall time, thread time, process time, self time

## Context Executor (`context_executor.py`)

ThreadPoolExecutor with automatic context propagation:
```python
from mindsdb.utilities.context_executor import ContextThreadPoolExecutor
executor = ContextThreadPoolExecutor(max_workers=4)
# Context variables automatically copied to worker threads
```

## Exception Types (`exception.py`)

```python
MindsDBError           # Base exception
EntityExistsError      # Entity already exists
EntityNotExistsError   # Entity not found
ParsingError           # SQL parsing errors
QueryError             # Database query errors (with detailed formatting)
```

## Security (`security.py`)

- `is_private_url(url)` - Check for private IP
- `clear_filename(filename)` - Sanitize path injection
- `validate_urls(urls, allowed, disallowed)` - Whitelist validation

## JSON Encoding (`json_encoder.py`)

`CustomJSONEncoder` handles: datetime, timedelta, Decimal, NumPy types, NaN

## Key Files

| File | Purpose |
|------|---------|
| `config.py` | Configuration management |
| `log.py` | Logging with sanitization |
| `context.py` | Thread-safe context |
| `cache.py` | Multi-backend caching |
| `ml_task_queue/` | Distributed ML tasks |
| `otel/` | OpenTelemetry integration |
| `profiler/` | Execution profiling |
| `exception.py` | Custom exceptions |
| `functions.py` | Common utilities |
