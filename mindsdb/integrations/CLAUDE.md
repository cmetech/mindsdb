# MindsDB Integrations Layer

## Overview
Handler framework for 100+ data source integrations. Each handler is a self-contained module providing connection, query execution, and metadata discovery.

## Handler Types

| Type | Base Class | Purpose |
|------|------------|---------|
| `HANDLER_TYPE.DATA` | `DatabaseHandler` | Databases, APIs, file systems |
| `HANDLER_TYPE.ML` | `BaseMLEngine` | ML frameworks, LLM providers |

## Handler Hierarchy

```
BaseHandler (abstract)
├── DatabaseHandler (data storage)
│   └── MetaDatabaseHandler (with data catalog)
├── APIHandler (REST APIs)
├── VectorStoreHandler (vector databases)
└── BaseMLEngine (ML frameworks)
```

## Handler Module Structure

Every handler in `handlers/{name}_handler/`:
```
{name}_handler/
├── __init__.py          # Exports and metadata
├── {name}_handler.py    # Main handler class
├── connection_args.py   # Connection parameters
├── __about__.py         # Package metadata
├── requirements.txt     # Dependencies (optional)
├── icon.svg            # UI icon
└── README.md           # Documentation
```

## Handler Registration Pattern

### `__init__.py` Template
```python
from mindsdb.integrations.libs.const import HANDLER_TYPE
from .__about__ import __version__ as version, __description__ as description

try:
    from .postgres_handler import PostgresHandler as Handler
    import_error = None
except Exception as e:
    Handler = None
    import_error = e

title = "PostgreSQL"
name = "postgres"
type = HANDLER_TYPE.DATA
icon_path = "icon.svg"
permanent = False
```

### Connection Args Pattern
```python
from collections import OrderedDict
from mindsdb.integrations.libs.const import HANDLER_CONNECTION_ARG_TYPE as ARG_TYPE

connection_args = OrderedDict(
    host={'type': ARG_TYPE.STR, 'description': 'Hostname', 'required': True},
    port={'type': ARG_TYPE.INT, 'description': 'Port', 'required': True},
    password={'type': ARG_TYPE.PWD, 'description': 'Password', 'secret': True},
)
```

## Handler Implementation

### DatabaseHandler Methods
```python
class PostgresHandler(DatabaseHandler):
    def connect(self) -> None:
        """Establish connection, set self.is_connected = True"""

    def check_connection(self) -> HandlerStatusResponse:
        """Test connection, return success/error status"""

    def query(self, query: ASTNode) -> HandlerResponse:
        """Execute SQL, return DataFrame in HandlerResponse"""

    def native_query(self, query: str) -> HandlerResponse:
        """Execute raw SQL string"""

    def get_tables(self) -> HandlerResponse:
        """List available tables"""

    def get_columns(self, table: str) -> HandlerResponse:
        """List columns for table"""
```

### ML Engine Methods
```python
class OpenAIHandler(BaseMLEngine):
    def create(self, target, args, **kwargs):
        """Create/train model"""

    def predict(self, df, args):
        """Run inference, return predictions DataFrame"""

    def describe(self, key=None):
        """Return model metadata"""
```

## Response Types (`libs/response.py`)

```python
class HandlerResponse:
    resp_type: RESPONSE_TYPE  # TABLE, OK, ERROR
    data_frame: pd.DataFrame
    error_message: str
    affected_rows: int

class HandlerStatusResponse:
    success: bool
    error_message: str
```

## Handler Discovery & Loading

1. **Discovery**: Scans `handlers/` directory, parses `__init__.py` via AST (no imports)
2. **Lazy Loading**: Handlers imported on first use
3. **Instantiation**: `HandlerClass(name, connection_data, file_storage, handler_storage)`
4. **Caching**: Handler instances cached in `HandlersCache`

## API Handler Framework (`libs/api_handler.py`)

For REST API integrations:
```python
class APITable:
    """Abstract base for API resources"""
    def select(self, query): ...
    def insert(self, query): ...

class APIHandler(BaseHandler):
    """Routes SQL to APITable methods"""
```

## Vector Database Framework (`libs/vectordatabase_handler.py`)

Standard schema for vector stores:
```python
TableField: id, content, embeddings, metadata, search_vector, distance
DistanceFunction: SQUARED_EUCLIDEAN, NEGATIVE_DOT_PRODUCT, COSINE_DISTANCE
```

## Utilities (`utilities/`)

| File | Purpose |
|------|---------|
| `handler_utils.py` | API key resolution, credentials |
| `sql_utils.py` | FilterCondition, SortColumn, query parsing |
| `query_traversal.py` | AST tree traversal |
| `rag/` | RAG pipeline (loaders, splitters, rerankers) |

## Handler Categories (219 handlers)

| Category | Examples |
|----------|----------|
| SQL Databases | postgres, mysql, sqlite, clickhouse, snowflake |
| NoSQL | mongodb, redis, cassandra, couchbase |
| Cloud Warehouses | bigquery, redshift, databricks, athena |
| APIs | github, stripe, slack, salesforce, shopify |
| Vector DBs | chromadb, pinecone, weaviate, milvus, qdrant |
| ML/LLM | openai, anthropic, huggingface, bedrock |
| Files | s3, gcs, local files, excel, csv |

## Adding a New Handler

1. Create directory: `handlers/{name}_handler/`
2. Implement handler class extending appropriate base
3. Define `connection_args.py` with parameter definitions
4. Create `__init__.py` with metadata exports
5. Add `__about__.py` with version/description
6. Add `requirements.txt` if handler has dependencies
7. Write `README.md` documentation
