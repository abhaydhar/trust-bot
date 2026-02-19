# TrustBot

LLM-powered agent that validates Neo4j call graphs against actual codebases.

**Trust, but verify** — ensuring that knowledge graph representations of code (call graphs, execution flows) remain accurate and in sync with the living codebase.

## Architecture

```
Neo4j Call Graph  ──→  Agent (LLM via LiteLLM)  ←──  Actual Codebase
                              │
                        Tools Layer
                     ┌────────┼────────┐
                 Neo4j     Filesystem   Index
                 Tool        Tool       Tool
                   │           │          │
              Neo4j DB    Local Files  ChromaDB
```

## Quick Start

### Prerequisites

- Python 3.11+
- Neo4j instance with your call graph data
- An LLM API key (OpenAI, Anthropic, Azure, etc.)

### Setup

```bash
# Clone and enter the project
cd trust-bot

# Create virtual environment
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # macOS/Linux

# Install dependencies
pip install -e .

# Configure environment
cp .env.example .env
# Edit .env with your Neo4j credentials and LLM API key
```

### Run

```bash
python -m trustbot.main
```

The web UI launches at `http://localhost:7860` (or the port set in `SERVER_PORT`).

### Run Tests

```bash
pip install -e ".[dev]"
pytest
```

## Project Structure

```
trustbot/
├── main.py               # Application entry point
├── config.py             # Settings via pydantic-settings
├── models/
│   ├── graph.py          # ExecutionFlow, Snippet, CallGraph
│   └── validation.py     # ValidationReport, NodeStatus, EdgeStatus
├── tools/
│   ├── base.py           # BaseTool, ToolRegistry (access control, audit logging)
│   ├── neo4j_tool.py     # Neo4j queries (execution flows, call graphs)
│   ├── filesystem_tool.py # File reading, search, function extraction
│   └── index_tool.py     # Semantic search over indexed codebase
├── indexing/
│   ├── chunker.py        # Regex-based function-level code chunking
│   ├── embedder.py       # Embedding generation via LiteLLM
│   └── pipeline.py       # Full indexing pipeline (chunk → embed → store)
├── agent/
│   ├── orchestrator.py   # LLM agent with tool-calling
│   └── prompts.py        # System prompts and templates
├── validation/
│   └── engine.py         # Pre-filter + batched LLM validation
└── ui/
    └── app.py            # Gradio web interface
```

## How It Works

1. **Input**: User provides an execution flow key
2. **Graph Retrieval**: Agent queries Neo4j for the execution flow and its call graph
3. **Node Validation** (no LLM): For each snippet node, checks if the file/function exists in the codebase
4. **Edge Validation** (LLM-powered): For each call edge, extracts the caller function body and verifies it actually calls the callee
5. **Output**: Structured validation report + conversational summary

### Batching Strategy

To handle large codebases within LLM context limits:

- **Targeted extraction**: Reads only the relevant function body, not whole files
- **Pre-filtering**: Cheap filesystem checks eliminate obvious mismatches before LLM calls
- **Edge-by-edge validation**: Each LLM call handles one caller function
- **Parallel execution**: Multiple LLM calls run concurrently (configurable limit)
- **Truncation**: Very large functions are truncated with head/tail preservation

## Configuration

Key settings in `.env`:

| Variable | Description | Default |
|---|---|---|
| `NEO4J_URI` | Neo4j connection URI | `bolt://localhost:7687` |
| `NEO4J_USER` | Neo4j username | `neo4j` |
| `NEO4J_PASSWORD` | Neo4j password | — |
| `LITELLM_MODEL` | LLM model for reasoning | `gpt-4o` |
| `LITELLM_EMBEDDING_MODEL` | Model for embeddings | `text-embedding-3-small` |
| `CODEBASE_ROOT` | Path to the codebase to validate | `./sample_codebase` |
| `MAX_CONCURRENT_LLM_CALLS` | Parallel LLM call limit | `5` |
| `SERVER_PORT` | Web UI port | `7860` |

### Troubleshooting

- **Port already in use**: If you see "only one usage of each socket address...", another TrustBot instance or process is using the port. Stop it first, or set `SERVER_PORT=7865` (or another free port) in `.env`.
- **AttributeError: 'NoneType' object has no attribute 'send'**: This can occur when closing the app on Windows (asyncio/ProactorEventLoop teardown). The app now performs graceful shutdown of Neo4j and other connections; if it still appears, it is usually harmless and can be ignored.
