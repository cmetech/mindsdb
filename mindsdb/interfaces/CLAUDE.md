# MindsDB Interfaces Layer

## Overview
Core business logic layer implementing the controller pattern. Each domain has a dedicated controller managing CRUD operations, relationships, and business rules.

## Domain Components

### AI/Agent System

| Component | Controller | Model | Purpose |
|-----------|------------|-------|---------|
| Agents | `AgentsController` | `db.Agents` | AI agents with LLM integration |
| Skills | `SkillsController` | `db.Skills` | Reusable agent capabilities |
| Knowledge Bases | `KnowledgeBaseController` | `db.KnowledgeBase` | RAG vector storage |
| Chatbots | `ChatBotController` | `db.ChatBots` | Conversational interfaces |

**Relationships**:
```
Agent ←→ Skills (many-to-many via AgentSkillsAssociation)
Agent → Model (LLM for inference)
Agent → KnowledgeBase (RAG context)
ChatBot → Agent (execution backend)
```

### Data Organization

| Component | Controller | Model | Purpose |
|-----------|------------|-------|---------|
| Projects | `ProjectController` | `db.Project` | Namespace for all entities |
| Databases | `DatabaseController` | - | Unified project/integration access |
| Integrations | `IntegrationController` | `db.Integration` | External data sources |
| Views | `ViewController` | `db.View` | Saved SQL queries |

### Operations

| Component | Controller | Model | Purpose |
|-----------|------------|-------|---------|
| Jobs | `JobsController` | `db.Jobs` | Scheduled query execution |
| Tasks | - | `db.Tasks` | Background task tracking |
| Triggers | - | `db.Triggers` | Event-driven execution |
| Models | `ModelController` | `db.Predictor` | ML model registry |

## Key Patterns

### Controller Dependency Injection
```python
class AgentsController:
    def __init__(
        self,
        project_controller: ProjectController = None,
        skills_controller: SkillsController = None,
        model_controller: ModelController = None,
    ):
        # Uses provided or creates default
```

### Soft Deletes
All major models use `deleted_at` column:
```python
# Active records
query.filter(Model.deleted_at == null())
# "Delete" = set timestamp
record.deleted_at = datetime.now()
```

### Multi-Tenancy
Every model includes `company_id`:
```python
query.filter(Model.company_id == ctx.company_id)
```

### JSON Configuration
Flexible settings via JSON columns:
- `Agents.params` - Agent configuration
- `Skills.params` - Skill settings
- `KnowledgeBase.params` - Embedding/reranking config
- `Jobs.params` - Job metadata

## Storage Layer (`storage/`)

### Database Models (`db.py`)
SQLAlchemy ORM with custom types:
- `Array` - Stored as delimited string
- `Json` - Stored as JSON text
- `SecretDataJson` - Encrypted via mind_castle

### File Storage (`fs.py`)
Artifact storage abstraction:
- Model weights and checkpoints
- Uploaded files
- Handler-specific data

## Agent Execution Flow
```
API Request
  → AgentsController.get_agent()
  → LangChainAgent.run()
      → SkillsTool (for each skill)
      → KnowledgeBase.retrieve() (RAG)
      → Model inference (LLM calls)
  → Response
```

## Knowledge Base Flow
```
Document Upload
  → FileController.save_file()
  → KnowledgeBaseController.add_data()
  → PreprocessorFactory (chunk/clean)
  → Embedding Model inference
  → VectorDatabase insert
```

## Job Scheduling
```python
# Cron-like syntax
"every 5 minutes"
"every day at 2:00"
"0 2 * * *"  # Standard cron

# Conditional execution
if_query_str = "SELECT COUNT(*) FROM table WHERE condition"
```

## Key Files by Complexity

| File | Size | Purpose |
|------|------|---------|
| `knowledge_base/controller.py` | 66KB | RAG management |
| `agents/langchain_agent.py` | 37KB | Agent execution |
| `database/integrations.py` | 34KB | Integration management |
| `agents/agents_controller.py` | 27KB | Agent CRUD |
| `database/projects.py` | 22KB | Project management |
| `storage/db.py` | 22KB | All ORM models |
