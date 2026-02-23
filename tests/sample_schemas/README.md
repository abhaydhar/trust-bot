# Sample Schema Files for DB Entity Checker Testing

Generated from Neo4j project_id=976, run_id=2416 (CardDemo COBOL application).

## Source Data
- **22 unique DatabaseEntity nodes** with **70+ DatabaseField nodes** (COBOL VSAM/SEQUENCE files)
- Data fetched from: `bolt://rapidx-neo4j-dev.southindia.cloudapp.azure.com:7687`

## Positive Match Files (exact match with Neo4j)
These files contain ALL 22 entities with their exact field names and data types.
Expected result: 22 MATCHED tables, 0 discrepancies.

- `positive_match.csv`
- `positive_match.json`
- `positive_match.xlsx`

## Negative / Discrepancy Files (intentional mismatches)
These files have deliberate differences from the Neo4j data:

| Discrepancy Type | Details |
|------------------|---------|
| **ONLY_IN_NEO4J tables** | `DALYREJS-FILE` and `HTML-FILE` removed from file (2 tables) |
| **ONLY_IN_DB tables** | `AUDIT-LOG-FILE` and `TEMP-BATCH-FILE` added (2 tables) |
| **TYPE_MISMATCH** | `ACCOUNT-FILE.FD-ACCT-ID` changed from `NA` to `NUMERIC(11)` |
| **ONLY_IN_DB column** | `CARD-FILE.FD-CARD-EXPIRY` added (extra column) |
| **ONLY_IN_NEO4J column** | `XREF-FILE.FD-XREF-FILLER` removed from file |

Expected result: 20 MATCHED + 2 ONLY_IN_DB + 2 ONLY_IN_NEO4J tables,
plus column-level discrepancies in ACCOUNT-FILE, CARD-FILE, XREF-FILE.

- `negative_discrepancies.csv`
- `negative_discrepancies.json`
- `negative_discrepancies.xlsx`

## Usage
1. Open the TrustBot UI, navigate to **Tab 6: DB Entity Checker**
2. Enter **Project ID: 976** and **Run ID: 2416**
3. Select **Flat File Upload** and upload one of these files
4. Click **Compare Entities**
