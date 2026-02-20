# TrustBot Quick Start Guide

## Prerequisites

- **Python 3.11 or 3.12** (Python 3.14 has ChromaDB compatibility issues)
- **Neo4j instance** with call graph data
- **LLM API key** (OpenAI, Anthropic, Azure, etc.)

---

## Installation Steps

### 1. Clone and Navigate to Project

```bash
cd c:\Abhay\trust-bot
```

### 2. Create Virtual Environment (Recommended)

```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # macOS/Linux
```

### 3. Install Dependencies

```bash
# Core dependencies
pip install -r requirements.txt

# Or install in editable mode with dev tools
pip install -e ".[dev]"
```

### 4. Configure Environment

Create or edit `.env` file:

```env
# Neo4j Configuration
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_password_here

# LLM Configuration
LITELLM_MODEL=gpt-4o
LITELLM_API_KEY=your_api_key_here
# LITELLM_API_BASE=  # Optional: custom API endpoint

# Codebase Settings
CODEBASE_ROOT=./sample_codebase
CHROMA_PERSIST_DIR=./data/chromadb

# Optional Settings
SERVER_PORT=7860
LOG_LEVEL=INFO
MAX_CONCURRENT_LLM_CALLS=5

# Optional: Enable browser tool for E2E tests
ENABLE_BROWSER_TOOL=false
```

### 5. Install Playwright (Optional - for browser tests)

```bash
pip install playwright
playwright install chromium
```

---

## Running the Application

### Start TrustBot

```bash
python -m trustbot.main
```

The web UI will launch at: **http://localhost:7860**

### What Happens on Startup:

1. **Tool initialization** – Connects to Neo4j, filesystem, ChromaDB
2. **Code Index build** – Scans codebase and builds function → file path index (SQLite)
3. **Gradio UI launch** – Web interface starts on port 7860

---

## Using the Application

### Tab 1: **Validate** (Legacy Single-Agent)

1. Enter **Project ID** and **Run ID**
2. Click **Validate All Flows**
3. View validation report with node/edge results

### Tab 2: **Agentic (Dual-Derivation)** 🆕

1. Enter **Execution Flow Key** (e.g., `EF-001`)
2. Click **Run Dual-Derivation**
3. View trust scores:
   - **Confirmed edges** – Found in both Neo4j and filesystem
   - **Phantom edges** – In Neo4j only (potential drift)
   - **Missing edges** – In filesystem only (not captured in KB)

### Tab 3: **Chat**

Ask TrustBot questions about execution flows or code using natural language.

### Tab 4: **Index**

- **Incremental Re-index** – Update code index for changed files
- **Full Re-index** – Rebuild entire index
- **Check Status** – View index statistics

---

## Running Tests

```bash
# Run all tests
pytest

# Run specific test modules
pytest tests/test_agentic.py -v
pytest tests/test_filesystem_tool.py -v

# Run with coverage
pytest --cov=trustbot tests/
```

---

## Troubleshooting

### Issue: ChromaDB/Pydantic Error

**Error:** `ConfigError: unable to infer type for attribute "chroma_server_nofile"`

**Solution:** Use Python 3.11 or 3.12. Python 3.14 has compatibility issues with ChromaDB.

```bash
# Check Python version
python --version

# Use py launcher to select version (Windows)
py -3.11 -m trustbot.main
```

### Issue: Port Already in Use

**Error:** `Address already in use`

**Solution:** Change port in `.env`:

```env
SERVER_PORT=7865
```

### Issue: Neo4j Connection Failed

**Error:** `ServiceUnavailable: Failed to establish connection`

**Solution:** 
1. Verify Neo4j is running: `neo4j status`
2. Check credentials in `.env`
3. Test connection: `neo4j://localhost:7687`

### Issue: LLM API Rate Limit

**Solution:** Adjust concurrent calls in `.env`:

```env
MAX_CONCURRENT_LLM_CALLS=3
```

---

## Advanced Usage

### Running Browser E2E Test

```bash
# Set environment variable
set ENABLE_BROWSER_TOOL=true  # Windows
export ENABLE_BROWSER_TOOL=true  # Linux/Mac

# Run browser test script
python scripts/run_browser_test.py
```

### Using Job Queue (Celery + Redis)

```bash
# Install Redis (Windows: chocolatey, Linux: apt/yum)
choco install redis-64

# Start Redis
redis-server

# Enable Celery in .env
ENABLE_CELERY=true
REDIS_URL=redis://localhost:6379/0

# Start Celery worker (future enhancement)
celery -A trustbot.worker worker --loglevel=info
```

---

## Architecture Overview

### Multi-Agent Pipeline

```
User Request (Flow Key)
    ↓
Agent 1 (Neo4j) ──→ Call Graph A ──┐
                                    ├──→ Verification Agent
Agent 2 (Filesystem) → Call Graph B ┘
                                    ↓
                            Trust Score Report
```

- **Agent 1:** Fetches from Neo4j only
- **Agent 2:** Builds from filesystem with tiered extraction (regex → LLM)
- **Verification:** Diffs graphs, classifies edges, computes trust scores

### Key Features

✅ **Dual-derivation validation** – No circular dependencies  
✅ **Tiered extraction** – Fast regex first, LLM when needed  
✅ **Code Index** – SQLite for fast function lookups  
✅ **Trust scoring** – Edge-level, node-level, flow-level  
✅ **Browser control** – Playwright for E2E testing  

---

## Quick Commands Reference

```bash
# Start application
python -m trustbot.main

# Run tests
pytest

# Build code index only
python -c "from trustbot.index import CodeIndex; CodeIndex().build()"

# Check Neo4j connection
python -c "from trustbot.tools.neo4j_tool import Neo4jTool; import asyncio; asyncio.run(Neo4jTool().initialize())"
```

---

## Next Steps

1. **Connect your Neo4j instance** with call graph data
2. **Point CODEBASE_ROOT** to your actual codebase
3. **Run validation** on an Execution Flow
4. **Review trust scores** and phantom/missing edges
5. **Iterate** – Fix drifts and re-validate

---

**For more details, see:** [README.md](README.md)
