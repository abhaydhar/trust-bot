# YAML Generation — Quality Checklist

## Top 5 Critical Rules (Must Pass Before Submission)

1. Root Snippet must have `root_type` attribute set.
2. Every `CALLS` relationship must have `global_execution_order` (integer).
3. Every node must have `execution_order` — no node should be missing this.
4. All snippets in the flow must have `PARTICIPATES_IN_FLOW` pointing to the correct execution flow node.
5. The snippet graph via `CALLS` must be fully connected — no orphan snippets.
6. If any new nodes or any new relationship are added , Blueprint needs to be informed.
7. If any file is missed during parsing , it would not reflect in yaml.
8. Execution order should be correct according to the codebase.

---

## 1. Nodes

| # | Checklist Item | Details | Priority |
|---|---------------|---------|----------|
| 1 | All required node types present | Snippet, DBCall, Calculation, ServiceCall, Database Entities, Input Entity, Input Interface, Job, Step, Variable , JclJob| Critical |
| 2 | Every node has `execution_order` | Integer; defines order within the flow | Critical |
| 3 | Root Snippet has `root_type` set | Must be present on the starting snippet of each execution flow |  Critical |
| 4 | Every node has `name` attribute | Should be unique and descriptive | Required |
| 5 | `short_summary` present on all snippets | Brief one-line description of what the snippet does | Required |
| 6 | `business_summary` present on all snippets | Business-facing description; no technical jargon | Required |
| 7 | `snippet` attribute present on snippet nodes | Actual code or logic captured |  Required |
| 8 | `topic` attribute present | High-level domain/topic classification |  Required |
| 9 | `function_name` present where applicable | On snippet Node |  Required |
| 10 | `meaning` attribute present | Variable not must have meaning |  Required |
| 11 | `visual_imagery_data` present | For nodes with visual representation metadata | Required |
| 12 | `table_names` present on DBCall nodes | List all DB tables accessed in the call | Critical |

---

## 2. Relationships

| # | Checklist Item | Details | Priority |
|---|---------------|---------|----------|
| 13 | `CALLS` connects all snippets in the flow | Every snippet (except root) must be reachable via CALLS from another snippet | Critical |
| 14 | `CALLS` has `global_execution_order` | Integer on every CALLS relationship — determines cross-snippet ordering | Critical |
| 15 | All snippets have `PARTICIPATES_IN_FLOW` | Each snippet must link to its specific execution flow node |  Critical |
| 16 | `CONTAINS_DB_CALLS` present | Snippet → DBCall where DB calls exist |  Required |
| 17 | `CONTAINS_CALCULATION` present | Snippet → Calculation where calculations exist |  Required |
| 18 | `CONTAINS_SERVICE_CALLS` present | Snippet → ServiceCall where service calls exist |  Required |
| 19 | `CONTAINS_INPUT` present | Snippet → Input Entity/Interface |  Required |
| 20 | `CONTAINS_MAP_FILE` present | Snippet → map file reference |  Required |
| 21 | `CONTAINS_DMN` present | Snippet → DMN node where decision logic exists |  Required |
| 22 | `HAS_STEP` present | Job → Step relationships for job nodes | Required |
| 23 | `Contains_variable` present | Snippet/Step → Variable for variable tracking | Required |

---

## 3. Execution Order

| # | Checklist Item | Details | Priority |
|---|---------------|---------|----------|
| 24 | `execution_order` is sequential and correct | No duplicates within same flow; must reflect actual runtime order |  Critical |
| 25 | `global_execution_order` on CALLS is consistent | Globally unique integers across all CALLS relationships in the flow |  Critical |
| 26 | Root snippet is first in execution order | `starts_flow ` (or lowest) for root snippet |  Critical |

---

## 4. Connectivity

| # | Checklist Item | Details | Priority |
|---|---------------|---------|----------|
| 27 | No orphan snippets | Every snippet connected via CALLS or is root |  Critical |
| 28 | All paths trace back to root | Traversing CALLS from any snippet must eventually reach root |  Critical |
| 29 | Execution flow node exists | A dedicated flow node must exist for PARTICIPATES_IN_FLOW to reference |  Required |