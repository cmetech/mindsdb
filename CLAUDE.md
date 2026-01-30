# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MindsDB is an AI federated query engine that enables humans, AI agents, and applications to get highly accurate answers across large-scale data sources. It follows a "Connect, Unify, Respond" philosophy with 100+ data source integrations.

**Python Support**: 3.10.x, 3.11.x, 3.12.x, 3.13.x

## Common Commands

### Installation & Setup
```bash
make install_mindsdb                    # Install in editable mode with dev requirements
make install_handler HANDLER_NAME=xxx   # Install specific handler (e.g., postgres, mysql)
```

### Running MindsDB
```bash
make run_mindsdb                        # Start MindsDB server
python -m mindsdb                       # Direct invocation
docker-compose up -d                    # Full local stack (MindsDB + Postgres)
```

### Testing
```bash
# Unit tests (executor tests run separately due to isolation requirements)
make unit_tests                         # Standard unit tests
make unit_tests_slow                    # Include slow tests (--runslow flag)
env PYTHONPATH=./ pytest tests/unit/executor/  # Executor tests only

# Run specific test file
pytest -v tests/unit/path/to/test_file.py

# Run specific test
pytest -v tests/unit/path/to/test_file.py::test_function_name

# Integration tests (auth tests run separately as they modify auth state)
make integration_tests

# Handler integration tests (DSI - Datasource Integration)
make datasource_integration_tests
```

### Code Quality
```bash
make check                              # Run all checks (requirements, print statements, pre-commit)
make precommit                          # Install and run pre-commit hooks
make format                             # Auto-format code with ruff
```

### Docker
```bash
make build_docker                       # Build Docker image
make run_docker                         # Build and run (port 47334)
```

## Architecture

### Directory Structure
```
mindsdb/
├── api/                    # Response Layer - HTTP, MySQL protocol, MCP, executor
│   ├── http/              # REST API endpoints
│   ├── mysql/             # MySQL wire protocol server
│   ├── executor/          # Query execution engine
│   └── mcp/               # Model Context Protocol support
├── interfaces/            # Unification Layer - core business logic
│   ├── agents/            # AI agent implementations
│   ├── knowledge_base/    # Unstructured data indexing
│   ├── model/             # ML model interface
│   ├── jobs/              # Task scheduling
│   └── database/          # Database abstraction
├── integrations/          # Connection Layer - 100+ data source handlers
│   └── handlers/          # Each handler is a self-contained integration
└── utilities/             # Shared utilities (config, logging, auth, cache)
```

### Handler Structure
Each integration handler (e.g., `postgres_handler/`) contains:
- `__about__.py` - Handler metadata and version
- `{name}_handler.py` - Main handler class extending `BaseHandler`
- `connection_args.py` - Connection parameter definitions
- `requirements.txt` - Handler-specific dependencies (optional)
- `README.md` - User documentation

## Code Standards

- **Linting**: Ruff with 120 character line length, Python 3.10+ target
- **No print statements**: Use logging framework instead (enforced by `tests/scripts/check_print_statements.py`)
- **Pre-commit required**: Hooks run ruff-check, ruff-format, and file validations

## Test Markers

- `@pytest.mark.slow` - Requires `--runslow` flag to run
- Auth tests run separately as they modify authentication requirements
- Executor tests require `PYTHONPATH=./` and run in isolation

## Key Services (docker-compose)

| Service | Port | Purpose |
|---------|------|---------|
| mindsdb | 47334 (HTTP), 47335 (MySQL) | Main server |
| postgres | 5432 | Test database |
