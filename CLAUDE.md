# CLAUDE.md - MindsDB Development Guide for Claude Code

This file provides comprehensive guidance to Claude Code when working with the MindsDB codebase and integrating it with OSCAR.

## Project Overview

MindsDB is an open-source platform that brings machine learning capabilities to databases through SQL interfaces. It follows a "Connect, Unify, Respond" philosophy:

- **Connect**: 100+ integrations with databases, data warehouses, and APIs
- **Unify**: SQL-based interface to query across all connected sources
- **Respond**: AI agents, predictions, and automated responses

## Architecture Overview

### Core Components

```
┌─────────────────────────────────────────────────────────────┐
│                        API Layer                             │
│  (HTTP REST, MySQL, PostgreSQL, MongoDB, MCP, LiteLLM, A2A) │
├─────────────────────────────────────────────────────────────┤
│                     Executor Layer                           │
│     (SQL Parsing, Query Planning, Execution Orchestra)       │
├─────────────────────────────────────────────────────────────┤
│                    Interface Layer                           │
│ (Model, Agent, Chatbot, Knowledge Base, Jobs Controllers)   │
├─────────────────────────────────────────────────────────────┤
│                   Integration Layer                          │
│        (Data Handlers, ML Handlers, Vector DB Handlers)     │
├─────────────────────────────────────────────────────────────┤
│                     Storage Layer                            │
│      (SQLAlchemy ORM, Model Storage, JSON Storage)          │
└─────────────────────────────────────────────────────────────┘
```

### Key Directories

- `/mindsdb/api/` - API implementations (HTTP, database protocols)
- `/mindsdb/integrations/handlers/` - All data and ML integrations
- `/mindsdb/interfaces/` - Core business logic controllers
- `/mindsdb/utilities/` - Shared utilities and configuration

## Development Patterns

### 1. Handler Development

All integrations follow the handler pattern:

```python
from mindsdb.integrations.libs.base import DatabaseHandler

class MyHandler(DatabaseHandler):
    def connect(self):
        # Establish connection
    
    def query(self, query: ASTNode) -> pd.DataFrame:
        # Execute query
    
    def get_tables(self) -> List[Dict]:
        # Return schema information
```

### 2. SQL Extensions

MindsDB extends SQL with ML operations:

```sql
-- Train a model
CREATE MODEL my_model
FROM database_name (
    SELECT * FROM table_name
)
PREDICT target_column;

-- Make predictions
SELECT * FROM my_model
WHERE feature1 = 'value';

-- Create agents
CREATE AGENT my_agent
USING model = 'gpt-4',
      skills = ['sql_generation', 'data_analysis'];
```

### 3. Process Architecture

MindsDB runs multiple processes:
- HTTP API (Flask) - Port 47334
- MySQL Protocol - Port 47335
- PostgreSQL Protocol - Port 55432
- MongoDB Protocol - Port 47336
- Job Scheduler
- ML Task Queue

## Common Development Tasks

### Adding a New Data Integration

1. Create handler in `/mindsdb/integrations/handlers/my_handler/`
2. Implement required methods (connect, query, get_tables)
3. Add `__about__.py` with metadata
4. Add `requirements.txt` for dependencies
5. Register in handler registry

### Adding a New ML Engine

1. Create handler inheriting from `BaseMLEngine`
2. Implement `create()` and `predict()` methods
3. Handle model storage using `ModelStorage` class
4. Support model versioning

### Working with the Query Engine

The query engine processes SQL through these steps:
1. Parse SQL to AST (`mindsdb_sql_parser`)
2. Create query plan (`QueryPlanner`)
3. Execute plan steps (`SQLQuery`)
4. Return results

### Testing

```bash
# Run specific tests
pytest tests/unit/test_my_feature.py

# Run integration tests
pytest tests/integration/

# Test a specific handler
pytest tests/unit/handlers/test_my_handler.py
```

## Configuration

### Environment Variables

```bash
# Database connection
MINDSDB_DB_CON=sqlite:///mindsdb.db

# API configuration
MINDSDB_HTTP_PORT=47334
MINDSDB_MYSQL_PORT=47335

# ML API keys
MINDSDB_OPENAI_API_KEY=sk-...
```

### Configuration Files

- `config.json` - User configuration
- `config.auto.json` - Runtime configuration
- Default values in code

Priority: CLI args > ENV vars > config.auto.json > config.json > defaults

## Key Features to Understand

### 1. Agents and Skills

Agents use LangChain for complex reasoning:
- SQL generation from natural language
- RAG (Retrieval Augmented Generation)
- Custom Python functions as skills
- Memory management for conversations

### 2. Knowledge Bases

Vector search implementation:
- Document chunking and embedding
- Multiple vector DB support
- Hybrid search (semantic + keyword)
- LLM-based reranking

### 3. Jobs and Automation

Scheduled task execution:
```sql
CREATE JOB my_job (
    INSERT INTO predictions
    SELECT * FROM my_model
    WHERE date = CURRENT_DATE
) EVERY 1 day;
```

### 4. Model Context Protocol (MCP)

Enables IDE integrations:
- Cursor
- VS Code
- Other MCP-compatible tools

## Integration with OSCAR

### Recommended Approach

1. **Deploy as Microservice**: Run MindsDB as a separate container
2. **Create OSCAR Handler**: Expose OSCAR data to MindsDB
3. **Proxy Through Middleware**: All requests via oscar-middleware
4. **Maintain Security**: Use OSCAR's auth system

### Key Integration Points

```python
# oscar-middleware integration
@router.post("/mindsdb/query")
async def execute_query(query: str):
    # Forward to MindsDB
    # Apply OSCAR permissions
    # Return results
```

### Use Cases for OSCAR

1. **Predictive Maintenance**: Predict server failures
2. **Alert Classification**: Auto-categorize alerts
3. **Anomaly Detection**: Identify unusual patterns
4. **Natural Language Queries**: "Show me all critical alerts from last week"
5. **Automated Reports**: Generate insights automatically

## Best Practices

1. **Error Handling**: Always handle connection failures gracefully
2. **Resource Management**: Close connections properly
3. **Async Operations**: Use async for I/O operations
4. **Caching**: Cache repeated predictions
5. **Security**: Never expose credentials in code

## Debugging Tips

1. **Enable Debug Logging**:
   ```bash
   export MINDSDB_LOG_LEVEL=DEBUG
   ```

2. **Check Process Status**:
   ```python
   # In __main__.py, processes are managed
   # Check logs for each process
   ```

3. **Query Execution**:
   - Add breakpoints in `sql_query.py`
   - Trace through query planner
   - Check step execution

4. **Handler Issues**:
   - Verify connection parameters
   - Check handler requirements
   - Test queries directly

## Common Issues

1. **Import Errors**: Handler dependencies not installed
2. **Connection Failures**: Check network and credentials
3. **Query Errors**: Validate SQL syntax and table names
4. **Model Training Failures**: Check data quality and parameters

## Performance Considerations

1. **Connection Pooling**: Reuse database connections
2. **Batch Predictions**: Process multiple rows together
3. **Async Execution**: Don't block on I/O
4. **Result Streaming**: For large datasets
5. **Model Caching**: Keep frequently used models in memory

## Security Guidelines

1. **Credential Storage**: Use environment variables or secrets manager
2. **API Authentication**: Always validate API keys
3. **SQL Injection**: Use parameterized queries
4. **Data Access**: Implement row-level security
5. **Audit Logging**: Track all operations

## Contributing

1. Follow existing code patterns
2. Add tests for new features
3. Update documentation
4. Run linters before committing
5. Keep handlers isolated and modular

## Additional Resources

- MindsDB Docs: https://docs.mindsdb.com
- SQL Parser: https://github.com/mindsdb/mindsdb_sql_parser
- Community Slack: https://mindsdb.com/joincommunity

## Notes for OSCAR Integration

When integrating with OSCAR:
1. Respect OSCAR's microservice architecture
2. Use oscar-middleware as the gateway
3. Maintain OSCAR's security model
4. Follow OSCAR's logging standards
5. Integrate with OSCAR's monitoring

Remember: MindsDB should enhance OSCAR's capabilities without compromising its core architecture or security model.