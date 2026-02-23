"""
Generate sample schema files for DB Entity Checker testing.

Reads real DatabaseEntity data from Neo4j (project_id=976, run_id=2416)
and produces:
  1. Positive-match files  (exact match with Neo4j)  — CSV, JSON, XLSX
  2. Negative-test files   (intentional discrepancies) — CSV, JSON, XLSX

Output directory: tests/sample_schemas/
"""

import csv
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Load and deduplicate real Neo4j data
# ---------------------------------------------------------------------------

raw = json.load(open("scripts/neo4j_db_entities_976.json"))

merged: dict[str, dict] = {}
for entry in raw:
    name = entry["entity"]["name"]
    if name not in merged:
        merged[name] = {"name": name, "fields": {}}
    for f in entry["fields"]:
        fname = f["name"]
        if fname not in merged[name]["fields"]:
            merged[name]["fields"][fname] = {
                "name": fname,
                "data_type": f.get("data_type", "NA"),
            }

# Build flat list: (table_name, column_name, data_type, is_nullable, is_primary_key)
positive_rows = []
for tname in sorted(merged.keys()):
    for fname in sorted(merged[tname]["fields"].keys()):
        finfo = merged[tname]["fields"][fname]
        positive_rows.append({
            "table_name": tname,
            "column_name": fname,
            "data_type": finfo["data_type"],
            "is_nullable": "true",
            "is_primary_key": "false",
        })

print(f"Positive dataset: {len(merged)} tables, {len(positive_rows)} total fields")

# ---------------------------------------------------------------------------
# Build negative test data (intentional discrepancies)
# ---------------------------------------------------------------------------

negative_rows = []
tables_in_negative = set()

for row in positive_rows:
    tname = row["table_name"]

    # REMOVE: skip DALYREJS-FILE and HTML-FILE entirely (simulate ONLY_IN_NEO4J)
    if tname in ("DALYREJS-FILE", "HTML-FILE"):
        continue

    # TYPE_MISMATCH: change data_type for some fields in ACCOUNT-FILE
    if tname == "ACCOUNT-FILE" and row["column_name"] == "FD-ACCT-ID":
        negative_rows.append({
            **row,
            "data_type": "NUMERIC(11)",  # mismatch: was "NA"
        })
        tables_in_negative.add(tname)
        continue

    # ONLY_IN_DB column: add extra field to CARD-FILE
    if tname == "CARD-FILE" and row["column_name"] == "FD-CARD-NUM":
        negative_rows.append(row)
        negative_rows.append({
            "table_name": tname,
            "column_name": "FD-CARD-EXPIRY",
            "data_type": "DATE",
            "is_nullable": "true",
            "is_primary_key": "false",
        })
        tables_in_negative.add(tname)
        continue

    # ONLY_IN_NEO4J column: skip FD-XREF-FILLER from XREF-FILE
    if tname == "XREF-FILE" and row["column_name"] == "FD-XREF-FILLER":
        tables_in_negative.add(tname)
        continue

    negative_rows.append(row)
    tables_in_negative.add(tname)

# ADD: extra tables that don't exist in Neo4j (simulate ONLY_IN_DB)
extra_tables = [
    {"table_name": "AUDIT-LOG-FILE", "column_name": "FD-AUDIT-ID", "data_type": "NUMERIC(9)", "is_nullable": "false", "is_primary_key": "true"},
    {"table_name": "AUDIT-LOG-FILE", "column_name": "FD-AUDIT-ACTION", "data_type": "VARCHAR(50)", "is_nullable": "false", "is_primary_key": "false"},
    {"table_name": "AUDIT-LOG-FILE", "column_name": "FD-AUDIT-TIMESTAMP", "data_type": "TIMESTAMP", "is_nullable": "false", "is_primary_key": "false"},
    {"table_name": "TEMP-BATCH-FILE", "column_name": "FD-BATCH-ID", "data_type": "NUMERIC(6)", "is_nullable": "false", "is_primary_key": "true"},
    {"table_name": "TEMP-BATCH-FILE", "column_name": "FD-BATCH-DATA", "data_type": "VARCHAR(500)", "is_nullable": "true", "is_primary_key": "false"},
]
negative_rows.extend(extra_tables)

neg_tables = set(r["table_name"] for r in negative_rows)
print(f"Negative dataset: {len(neg_tables)} tables, {len(negative_rows)} total fields")
print(f"  Removed tables (ONLY_IN_NEO4J): DALYREJS-FILE, HTML-FILE")
print(f"  Added tables (ONLY_IN_DB): AUDIT-LOG-FILE, TEMP-BATCH-FILE")
print(f"  Type mismatch: ACCOUNT-FILE.FD-ACCT-ID (NA -> NUMERIC(11))")
print(f"  Extra column (ONLY_IN_DB): CARD-FILE.FD-CARD-EXPIRY")
print(f"  Missing column (ONLY_IN_NEO4J): XREF-FILE.FD-XREF-FILLER")

# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------

out = Path("tests/sample_schemas")
out.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# CSV files
# ---------------------------------------------------------------------------

FIELDNAMES = ["table_name", "column_name", "data_type", "is_nullable", "is_primary_key"]


def write_csv(rows, filepath):
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Written: {filepath}")


write_csv(positive_rows, out / "positive_match.csv")
write_csv(negative_rows, out / "negative_discrepancies.csv")

# ---------------------------------------------------------------------------
# JSON files
# ---------------------------------------------------------------------------


def rows_to_json_tables(rows):
    tables = {}
    for r in rows:
        tname = r["table_name"]
        if tname not in tables:
            tables[tname] = {"name": tname, "columns": []}
        tables[tname]["columns"].append({
            "name": r["column_name"],
            "data_type": r["data_type"],
            "is_nullable": r["is_nullable"].lower() in ("true", "yes", "1"),
            "is_primary_key": r["is_primary_key"].lower() in ("true", "yes", "1"),
        })
    return {"tables": list(tables.values())}


def write_json(rows, filepath):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(rows_to_json_tables(rows), f, indent=2)
    print(f"  Written: {filepath}")


write_json(positive_rows, out / "positive_match.json")
write_json(negative_rows, out / "negative_discrepancies.json")

# ---------------------------------------------------------------------------
# Excel files
# ---------------------------------------------------------------------------

try:
    from openpyxl import Workbook

    def write_excel(rows, filepath):
        wb = Workbook()
        ws = wb.active
        ws.title = "Schema"
        ws.append(FIELDNAMES)
        for r in rows:
            ws.append([r[k] for k in FIELDNAMES])
        wb.save(filepath)
        print(f"  Written: {filepath}")

    write_excel(positive_rows, out / "positive_match.xlsx")
    write_excel(negative_rows, out / "negative_discrepancies.xlsx")

except ImportError:
    print("  SKIP: openpyxl not installed, skipping .xlsx generation")

# ---------------------------------------------------------------------------
# Summary README
# ---------------------------------------------------------------------------

readme = f"""# Sample Schema Files for DB Entity Checker Testing

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
"""

with open(out / "README.md", "w", encoding="utf-8") as f:
    f.write(readme)
print(f"  Written: {out / 'README.md'}")

print("\nDone! All sample files generated in tests/sample_schemas/")
