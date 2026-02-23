# PRD: Database Entity Checker

**Author:** TrustBot Team  
**Date:** February 23, 2026  
**Status:** Draft  
**Version:** 1.0

---

## 1. Overview

### 1.1 Problem Statement

TrustBot currently validates **execution flows** (call graphs) by comparing Neo4j-stored graphs against indexed codebase graphs. However, the Neo4j knowledge graph also contains **database entity** information — `DatabaseEntity` nodes representing tables and `DatabaseField` nodes representing columns, linked via `HAS_FIELD` relationships.

There is no mechanism today for users to verify that these Neo4j database entities actually match the real database schema. A mismatch could indicate:

- Stale or incomplete Neo4j data from a previous extraction run.
- Tables/columns added or removed in the database after the Neo4j extraction.
- Extraction bugs that missed or duplicated entities.

### 1.2 Proposed Solution

Add a new **"6. DB Entity Checker"** tab to TrustBot that allows users to:

1. **Connect to a PostgreSQL database** with user-provided credentials (host, port, database, schema, username, password) to retrieve the live schema.
2. **Alternatively, upload a flat file** (CSV, JSON, or Excel) containing schema information — for environments where live database connectivity is not feasible (e.g., air-gapped networks, restricted production databases).
3. **Query Neo4j** for `DatabaseEntity` and `DatabaseField` nodes filtered by `project_id` and `run_id`.
4. **Compare** both sources and display discrepancies at the table level and column level (drill-down).

### 1.3 Goals

- Enable TRUST verification for database entities in Neo4j by comparing against the actual database schema.
- Support PostgreSQL live connections for real-time verification.
- Provide a flat-file fallback (CSV, JSON, Excel) for environments where live DB access is unavailable.
- Display results in an intuitive UI with summary statistics, color-coded status indicators, and drill-down capability.
- Reuse the existing Neo4j connection pool and retry infrastructure from `Neo4jTool`.

---

## 2. User Stories

| # | As a... | I want to... | So that... |
|---|---------|-------------|------------|
| US-1 | TrustBot user | Connect to my PostgreSQL database from within TrustBot | I can verify Neo4j entities match the real schema |
| US-2 | TrustBot user | Upload a flat file with schema information | I can verify entities even when live DB access is not possible |
| US-3 | TrustBot user | See all tables and columns from both the database and Neo4j | I can understand what each source contains |
| US-4 | TrustBot user | See which tables exist in the DB but not in Neo4j (and vice versa) | I can identify extraction gaps or stale data |
| US-5 | TrustBot user | Drill down into matched tables to see column-level discrepancies | I can find fine-grained mismatches |
| US-6 | TrustBot user | See a summary with counts (matched, missing, extra) | I get an at-a-glance view of schema alignment |
| US-7 | TrustBot user | Use CSV, JSON, or Excel formats for flat file upload | I can use whatever format my DBA provides |

---

## 3. Functional Requirements

### 3.1 New Tab

| ID | Requirement |
|----|-------------|
| FR-1 | Add a new tab **"6. DB Entity Checker"** to the existing NiceGUI tab bar. |
| FR-2 | The tab appears after "5. Index Management" in the tab order. |

### 3.2 Project/Run Inputs

| ID | Requirement |
|----|-------------|
| FR-3 | Provide **Project ID** and **Run ID** input fields (integers) to filter Neo4j `DatabaseEntity` nodes. |
| FR-4 | These inputs are required; clicking "Compare Entities" without them shows an error message. |

### 3.3 Source Selection

| ID | Requirement |
|----|-------------|
| FR-5 | Add a **radio button group** with two options: **"PostgreSQL Connection"** (default) and **"Flat File Upload"**. |
| FR-6 | When **"PostgreSQL Connection"** is selected, show credential input fields. When **"Flat File Upload"** is selected, show file upload widget. |

### 3.4 PostgreSQL Connection

| ID | Requirement |
|----|-------------|
| FR-7 | Provide input fields for: **Host**, **Port** (default: `5432`), **Database**, **Schema** (default: `public`), **Username**, **Password** (masked). |
| FR-8 | Credentials are entered fresh each session — not persisted to storage. |
| FR-9 | Connection timeout is 30 seconds. On failure, display error with host/port/db (password masked). |
| FR-10 | Query `information_schema.tables` and `information_schema.columns` for the given schema to retrieve table names and column metadata. |

### 3.5 Flat File Upload

| ID | Requirement |
|----|-------------|
| FR-11 | Provide a file upload widget accepting `.csv`, `.json`, and `.xlsx` files. |
| FR-12 | **CSV format**: columns `table_name, column_name, data_type, is_nullable, is_primary_key`. |
| FR-13 | **JSON format**: `{"tables": [{"name": "...", "columns": [{"name": "...", "data_type": "...", ...}]}]}`. |
| FR-14 | **Excel format**: single sheet with same columns as CSV. |
| FR-15 | Auto-detect format by file extension. On parse error, show clear error message. |

### 3.6 Neo4j Query

| ID | Requirement |
|----|-------------|
| FR-16 | Query Neo4j for all `DatabaseEntity` nodes matching the given `project_id` and `run_id`. |
| FR-17 | For each `DatabaseEntity`, traverse `HAS_FIELD` relationships to collect `DatabaseField` nodes. |
| FR-18 | Expected `DatabaseEntity` properties: `name` (table name), `schema_name`, `project_id`, `run_id`. |
| FR-19 | Expected `DatabaseField` properties: `name` (column name), `data_type`, `is_nullable`, `is_primary_key`. |
| FR-20 | If no `DatabaseEntity` nodes are found, show an informational message but still display the DB schema. |

### 3.7 Comparison Logic

| ID | Requirement |
|----|-------------|
| FR-21 | Compare table names (case-insensitive) between DB schema and Neo4j entities. |
| FR-22 | Classify each table as **MATCHED**, **ONLY_IN_DB**, or **ONLY_IN_NEO4J**. |
| FR-23 | For MATCHED tables, compare column names (case-insensitive). |
| FR-24 | Classify each column as **MATCHED**, **ONLY_IN_DB**, **ONLY_IN_NEO4J**, or **TYPE_MISMATCH**. |

### 3.8 Results Display

| ID | Requirement |
|----|-------------|
| FR-25 | Display results in four sub-tabs: **Summary**, **Database Tables**, **Neo4j Entities**, **Discrepancies**. |
| FR-26 | **Summary** sub-tab: Show stat cards (total tables, matched, only-in-DB, only-in-Neo4j) and a color-coded table-level summary. |
| FR-27 | **Database Tables** sub-tab: Show all DB tables with their columns in an expandable list. |
| FR-28 | **Neo4j Entities** sub-tab: Show all Neo4j `DatabaseEntity` nodes with their `DatabaseField` nodes. |
| FR-29 | **Discrepancies** sub-tab: Show only tables/columns with mismatches, with expandable per-table column diff. |
| FR-30 | Color coding: green = matched, orange/amber = only in DB, red = only in Neo4j, yellow = type mismatch. |

---

## 4. Technical Design

### 4.1 New Files

| File | Purpose |
|------|---------|
| `trustbot/models/db_entity.py` | Pydantic models for DB tables, columns, Neo4j entities, fields, comparison results |
| `trustbot/tools/db_schema_tool.py` | PostgreSQL connector + flat file parser (CSV/JSON/Excel) |
| `trustbot/tools/neo4j_entity_tool.py` | Neo4j queries for DatabaseEntity/DatabaseField nodes |
| `trustbot/services/schema_comparator.py` | Comparison logic: table-level and column-level matching |

### 4.2 Modified Files

| File | Change |
|------|--------|
| `trustbot/ui/app.py` | Add Tab 6 with all UI components, async handlers, result display |
| `requirements.txt` | Add `psycopg[binary]>=3.1.0` and `openpyxl>=3.1.0` |

### 4.3 Architecture

```
Tab 6: DB Entity Checker
├── Input: Project ID + Run ID
├── Source: PostgreSQL Connection OR Flat File Upload
│
├── PostgreSQL path:
│   ├── User provides: host, port, db, schema, username, password
│   ├── psycopg connects and queries information_schema
│   └── Returns list[DatabaseTable]
│
├── Flat file path:
│   ├── User uploads CSV, JSON, or Excel
│   ├── Parser auto-detects format
│   └── Returns list[DatabaseTable]
│
├── Neo4j path:
│   ├── Reuses existing Neo4jTool driver
│   ├── Cypher: MATCH (e:DatabaseEntity) -[:HAS_FIELD]-> (f:DatabaseField)
│   └── Returns list[Neo4jDatabaseEntity]
│
├── Schema Comparator:
│   ├── Table-level: MATCHED / ONLY_IN_DB / ONLY_IN_NEO4J
│   └── Column-level: MATCHED / ONLY_IN_DB / ONLY_IN_NEO4J / TYPE_MISMATCH
│
└── Results UI:
    ├── Summary (stat cards + overview table)
    ├── Database Tables (expandable)
    ├── Neo4j Entities (expandable)
    └── Discrepancies (expandable with color coding)
```

---

## 5. UI Mockup (Text)

### 5.1 State: PostgreSQL Connection Selected (Default)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  6. DB Entity Checker                                                    │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ### Database Entity Verification                                        │
│  Verify that database entities in Neo4j match the actual database        │
│  schema. Connect to PostgreSQL or upload a flat file.                    │
│                                                                          │
│  ┌──────────────────┐ ┌──────────────────┐                               │
│  │ Project ID       │ │ Run ID           │                               │
│  │ 3151             │ │ 4912             │                               │
│  └──────────────────┘ └──────────────────┘                               │
│                                                                          │
│  Source:  (●) PostgreSQL Connection  ( ) Flat File Upload                │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────┐        │
│  │  ┌──────────────┐ ┌────────┐                                │        │
│  │  │ Host         │ │ Port   │                                │        │
│  │  │ db-server    │ │ 5432   │                                │        │
│  │  └──────────────┘ └────────┘                                │        │
│  │  ┌──────────────┐ ┌────────────┐                            │        │
│  │  │ Database     │ │ Schema     │                            │        │
│  │  │ mydb         │ │ public     │                            │        │
│  │  └──────────────┘ └────────────┘                            │        │
│  │  ┌──────────────┐ ┌────────────┐                            │        │
│  │  │ Username     │ │ Password   │                            │        │
│  │  │ admin        │ │ ••••••••   │                            │        │
│  │  └──────────────┘ └────────────┘                            │        │
│  └──────────────────────────────────────────────────────────────┘        │
│                                                                          │
│  ┌──────────────────────┐                                                │
│  │  Compare Entities    │                                                │
│  └──────────────────────┘                                                │
│                                                                          │
│  ─────────────────────────────────────────────────────────────           │
│                                                                          │
│  [ Summary ] [ Database Tables ] [ Neo4j Entities ] [ Discrepancies ]   │
│                                                                          │
│  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐                                │
│  │  25  │  │  20  │  │  3   │  │  2   │                                │
│  │Total │  │Match │  │DB    │  │Neo4j │                                │
│  │Tables│  │      │  │Only  │  │Only  │                                │
│  └──────┘  └──────┘  └──────┘  └──────┘                                │
│                                                                          │
│  | Table Name  | Status       |                                         │
│  |-------------|--------------|                                         │
│  | users       | MATCHED      |  (green)                                │
│  | orders      | MATCHED      |  (green)                                │
│  | temp_cache  | ONLY IN DB   |  (orange)                               │
│  | audit_log   | ONLY IN NEO4J|  (red)                                  │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 5.2 State: Flat File Upload Selected

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Source:  ( ) PostgreSQL Connection  (●) Flat File Upload                │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────┐        │
│  │  [ Upload schema file ]  (.csv, .json, .xlsx)               │        │
│  │                                                              │        │
│  │  Supported formats: CSV, JSON, Excel (.xlsx)                │        │
│  │                                                              │        │
│  │  ▶ Sample CSV format                                        │        │
│  │    table_name,column_name,data_type,is_nullable,is_primary  │        │
│  │    users,id,integer,false,true                               │        │
│  │    users,name,varchar,false,false                            │        │
│  └──────────────────────────────────────────────────────────────┘        │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 6. Detailed Behavior Specification

### 6.1 Source Toggle Behavior

| Action | Result |
|--------|--------|
| Page loads | "PostgreSQL Connection" is selected by default. Credential fields visible. File upload hidden. |
| User clicks "Flat File Upload" | Credential fields hide. File upload widget appears. |
| User clicks "PostgreSQL Connection" | File upload hides. Credential fields reappear (preserving values). |

### 6.2 Compare Entities Button Behavior

| Source | Validation | Action |
|--------|-----------|--------|
| PostgreSQL | Project ID, Run ID, Host, Database, Username, Password must be non-empty | Connect to PostgreSQL + query Neo4j |
| Flat File | Project ID, Run ID must be non-empty; file must be uploaded | Parse file + query Neo4j |

### 6.3 Results Sub-Tab Behavior

| Sub-tab | Content |
|---------|---------|
| Summary | Stat cards (total, matched, only-in-DB, only-in-Neo4j), color-coded overview table |
| Database Tables | All DB tables with expandable column lists |
| Neo4j Entities | All Neo4j DatabaseEntity nodes with expandable DatabaseField lists |
| Discrepancies | Only tables with mismatches; expandable per-table column diff with color coding |

### 6.4 Error Messages

| Condition | Message |
|-----------|---------|
| Project ID or Run ID empty | "Please enter both Project ID and Run ID." |
| Project ID or Run ID not integer | "Project ID and Run ID must be integers." |
| PostgreSQL: Host empty | "Please enter the database host." |
| PostgreSQL: Database empty | "Please enter the database name." |
| PostgreSQL: Username empty | "Please enter the database username." |
| PostgreSQL: Connection failed | "Failed to connect to PostgreSQL at {host}:{port}/{db}: {error}" |
| PostgreSQL: Connection timeout | "Connection timed out after 30 seconds." |
| Flat File: No file uploaded | "Please upload a schema file." |
| Flat File: Unsupported format | "Unsupported file format: {ext}. Use .csv, .json, or .xlsx." |
| Flat File: Parse error | "Failed to parse {filename}: {error}" |
| Neo4j: No entities found | "No DatabaseEntity nodes found for project_id={pid}, run_id={rid}." (info, not error) |
| Neo4j: Connection error | "Failed to connect to Neo4j: {error}" |

---

## 7. Data Flow Diagram

```
                        ┌──────────────┐
                        │   User       │
                        └──────┬───────┘
                               │ enters Project ID, Run ID, source
                               ▼
                    ┌─────────────────────┐
                    │  Source = ?          │
                    └──────┬──────┬───────┘
      "PostgreSQL"         │      │   "Flat File"
                           ▼      ▼
          ┌────────────────────┐  ┌─────────────────────┐
          │ psycopg connect    │  │ parse CSV/JSON/XLSX  │
          │ query info_schema  │  │ via file upload      │
          └────────┬───────────┘  └──────────┬──────────┘
                   │                         │
                   ▼                         ▼
          ┌──────────────────────────────────────────┐
          │  list[DatabaseTable]                      │
          │  (unified model from either source)       │
          └────────────────────┬─────────────────────┘
                               │
                               │         ┌──────────────────────────┐
                               │         │ Neo4j: DatabaseEntity     │
                               │         │ + HAS_FIELD → DatabaseField│
                               │         │ filtered by pid + rid     │
                               │         └────────────┬─────────────┘
                               │                      │
                               ▼                      ▼
                    ┌─────────────────────────────────────────┐
                    │  Schema Comparator                       │
                    │  Step 1: table-level match               │
                    │  Step 2: column-level drill-down         │
                    └────────────────────┬────────────────────┘
                                         │
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │  Results UI                              │
                    │  ├── Summary (stats + overview table)    │
                    │  ├── Database Tables (expandable)        │
                    │  ├── Neo4j Entities (expandable)         │
                    │  └── Discrepancies (color-coded diff)    │
                    └─────────────────────────────────────────┘
```

---

## 8. Edge Cases

| # | Scenario | Expected Behavior |
|---|----------|-------------------|
| EC-1 | Database schema has 0 tables | Comparison shows all Neo4j entities as ONLY_IN_NEO4J. |
| EC-2 | Neo4j has 0 DatabaseEntity nodes | Info message displayed. All DB tables shown as ONLY_IN_DB. |
| EC-3 | Both sources are empty | Summary shows 0 total, no discrepancies. |
| EC-4 | Table names differ only in case (e.g., "Users" vs "users") | Treated as a match (case-insensitive comparison). |
| EC-5 | Flat file has duplicate table entries | Deduplicated by table name; columns merged. |
| EC-6 | Very large schema (1000+ tables) | Pagination in result tables. No performance issue expected. |
| EC-7 | PostgreSQL password contains special characters | Handled by psycopg parameterized connection. |
| EC-8 | Flat file has missing columns (e.g., no is_primary_key) | Missing columns default to empty/false. |
| EC-9 | Neo4j DatabaseField has no data_type | data_type defaults to empty string; no error. |
| EC-10 | User uploads wrong file type (e.g., .pdf) | Error: "Unsupported file format: .pdf. Use .csv, .json, or .xlsx." |
| EC-11 | PostgreSQL connection drops mid-query | Error displayed: "Connection to PostgreSQL lost: {error}" |
| EC-12 | User changes source from PostgreSQL to Flat File after entering credentials | Credentials preserved but hidden; file upload appears. |

---

## 9. Acceptance Criteria

| # | Criterion | Pass Condition |
|---|-----------|---------------|
| AC-1 | Tab 6 is visible | "6. DB Entity Checker" tab appears in the tab bar |
| AC-2 | Project ID and Run ID inputs work | Integer validation, error messages for invalid input |
| AC-3 | Source toggle works | Switching between PostgreSQL and Flat File shows/hides correct sections |
| AC-4 | PostgreSQL connection works | Valid credentials connect and retrieve schema |
| AC-5 | PostgreSQL error handling | Invalid credentials show clear error (password masked) |
| AC-6 | CSV flat file upload works | Uploading a valid CSV parses tables and columns correctly |
| AC-7 | JSON flat file upload works | Uploading a valid JSON parses tables and columns correctly |
| AC-8 | Excel flat file upload works | Uploading a valid .xlsx parses tables and columns correctly |
| AC-9 | Neo4j query returns entities | DatabaseEntity + DatabaseField nodes retrieved for given pid/rid |
| AC-10 | Comparison logic correct | MATCHED, ONLY_IN_DB, ONLY_IN_NEO4J classifications are accurate |
| AC-11 | Column-level drill-down works | Clicking a matched table shows column comparison |
| AC-12 | Summary stats correct | Counts match the actual comparison results |
| AC-13 | Color coding visible | Green for matched, orange for DB-only, red for Neo4j-only |
| AC-14 | Existing tabs unaffected | Tabs 1-5 continue to work without changes |

---

## 10. Out of Scope

- **Database write operations**: The tool only reads schema metadata. No DDL or DML.
- **Non-PostgreSQL databases**: MySQL, Oracle, SQL Server support deferred to future versions.
- **Schema migration/sync**: The tool identifies discrepancies but does not fix them.
- **Neo4j write-back**: Comparison results are not written back to Neo4j.
- **Credential persistence**: DB credentials are not saved between sessions for security.
- **Automated/scheduled comparison**: Only on-demand via button click.

---

## 11. Implementation Estimate

| Task | Effort |
|------|--------|
| Create Pydantic models (`db_entity.py`) | ~20 min |
| Implement PostgreSQL connector (`db_schema_tool.py`) | ~45 min |
| Implement flat file parsers (CSV, JSON, Excel) | ~45 min |
| Implement Neo4j entity queries (`neo4j_entity_tool.py`) | ~30 min |
| Implement schema comparator (`schema_comparator.py`) | ~40 min |
| Build Tab 6 UI in `app.py` | ~90 min |
| Add dependencies to `requirements.txt` | ~5 min |
| Manual testing (all paths) | ~45 min |
| **Total** | **~5.5 hours** |

---

## 12. Future Enhancements

- **Additional database support**: MySQL, Oracle, SQL Server connectors.
- **Schema diff export**: Export comparison results as CSV/PDF report.
- **Trust score for entities**: Compute a numeric trust score (similar to call graph trust) for entity alignment.
- **Historical comparison**: Track entity comparisons over time to detect drift.
- **Auto-detect schema**: When connecting to PostgreSQL, list available schemas in a dropdown.
- **Bi-directional sync suggestions**: Suggest Cypher queries to add missing entities to Neo4j.
