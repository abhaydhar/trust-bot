"""Tests for the DB Entity Checker feature.

Covers:
  - Pydantic models (db_entity.py)
  - Flat file parsers: CSV, JSON, Excel (db_schema_tool.py)
  - Schema comparator logic (schema_comparator.py)
  - Neo4j entity tool deduplication (neo4j_entity_tool.py)
  - End-to-end integration with sample files from tests/sample_schemas/
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

SAMPLE_DIR = Path(__file__).parent / "sample_schemas"


# ═══════════════════════════════════════════════════════════════════════════
# 1. Model tests
# ═══════════════════════════════════════════════════════════════════════════


class TestModels:

    def test_database_column_defaults(self):
        from trustbot.models.db_entity import DatabaseColumn
        col = DatabaseColumn(name="id")
        assert col.name == "id"
        assert col.data_type == ""
        assert col.is_nullable is True
        assert col.is_primary_key is False

    def test_database_table_with_columns(self):
        from trustbot.models.db_entity import DatabaseColumn, DatabaseTable
        tbl = DatabaseTable(
            name="users",
            schema_name="public",
            columns=[
                DatabaseColumn(name="id", data_type="integer", is_primary_key=True),
                DatabaseColumn(name="email", data_type="varchar"),
            ],
        )
        assert tbl.name == "users"
        assert len(tbl.columns) == 2
        assert tbl.columns[0].is_primary_key is True

    def test_neo4j_database_entity_with_fields(self):
        from trustbot.models.db_entity import Neo4jDatabaseEntity, Neo4jDatabaseField
        entity = Neo4jDatabaseEntity(
            name="ACCOUNT-FILE",
            schema_name="Public",
            project_id=976,
            run_id=2416,
            fields=[
                Neo4jDatabaseField(name="FD-ACCT-ID", data_type="NA"),
                Neo4jDatabaseField(name="FD-ACCT-DATA", data_type="NA"),
            ],
        )
        assert entity.name == "ACCOUNT-FILE"
        assert entity.project_id == 976
        assert len(entity.fields) == 2

    def test_column_discrepancy(self):
        from trustbot.models.db_entity import ColumnDiscrepancy
        disc = ColumnDiscrepancy(
            column_name="FD-ACCT-ID",
            status="TYPE_MISMATCH",
            db_type="NUMERIC(11)",
            neo4j_type="NA",
        )
        assert disc.status == "TYPE_MISMATCH"
        assert disc.db_type == "NUMERIC(11)"

    def test_schema_comparison_summary_defaults(self):
        from trustbot.models.db_entity import SchemaComparisonSummary
        summary = SchemaComparisonSummary()
        assert summary.total_tables == 0
        assert summary.matched_tables == 0
        assert summary.results == []


# ═══════════════════════════════════════════════════════════════════════════
# 2. Flat file parser tests
# ═══════════════════════════════════════════════════════════════════════════


class TestCSVParser:

    def test_parse_positive_csv(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        content = (SAMPLE_DIR / "positive_match.csv").read_bytes()
        tables = parse_schema_file("positive_match.csv", content)
        assert len(tables) == 22
        table_names = {t.name for t in tables}
        assert "ACCOUNT-FILE" in table_names
        assert "XREF-FILE" in table_names

    def test_parse_negative_csv(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        content = (SAMPLE_DIR / "negative_discrepancies.csv").read_bytes()
        tables = parse_schema_file("negative_discrepancies.csv", content)
        table_names = {t.name for t in tables}
        assert "DALYREJS-FILE" not in table_names
        assert "HTML-FILE" not in table_names
        assert "AUDIT-LOG-FILE" in table_names
        assert "TEMP-BATCH-FILE" in table_names

    def test_csv_column_counts(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        content = (SAMPLE_DIR / "positive_match.csv").read_bytes()
        tables = parse_schema_file("positive_match.csv", content)
        by_name = {t.name: t for t in tables}
        assert len(by_name["ACCOUNT-FILE"].columns) == 3
        assert len(by_name["XREF-FILE"].columns) == 7
        assert len(by_name["DISCGRP-FILE"].columns) == 6

    def test_csv_negative_has_extra_column(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        content = (SAMPLE_DIR / "negative_discrepancies.csv").read_bytes()
        tables = parse_schema_file("negative_discrepancies.csv", content)
        by_name = {t.name: t for t in tables}
        card_cols = {c.name for c in by_name["CARD-FILE"].columns}
        assert "FD-CARD-EXPIRY" in card_cols

    def test_csv_negative_type_mismatch(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        content = (SAMPLE_DIR / "negative_discrepancies.csv").read_bytes()
        tables = parse_schema_file("negative_discrepancies.csv", content)
        by_name = {t.name: t for t in tables}
        acct_cols = {c.name: c for c in by_name["ACCOUNT-FILE"].columns}
        assert acct_cols["FD-ACCT-ID"].data_type == "NUMERIC(11)"

    def test_empty_csv(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        content = b"table_name,column_name,data_type,is_nullable,is_primary_key\n"
        tables = parse_schema_file("empty.csv", content)
        assert tables == []

    def test_csv_with_missing_optional_columns(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        content = b"table_name,column_name,data_type\nusers,id,integer\n"
        tables = parse_schema_file("minimal.csv", content)
        assert len(tables) == 1
        assert tables[0].columns[0].is_nullable is True
        assert tables[0].columns[0].is_primary_key is False


class TestJSONParser:

    def test_parse_positive_json(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        content = (SAMPLE_DIR / "positive_match.json").read_bytes()
        tables = parse_schema_file("positive_match.json", content)
        assert len(tables) == 22

    def test_parse_negative_json(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        content = (SAMPLE_DIR / "negative_discrepancies.json").read_bytes()
        tables = parse_schema_file("negative_discrepancies.json", content)
        table_names = {t.name for t in tables}
        assert "AUDIT-LOG-FILE" in table_names
        assert "DALYREJS-FILE" not in table_names

    def test_json_list_format(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        data = [{"name": "t1", "columns": [{"name": "c1", "data_type": "int"}]}]
        content = json.dumps(data).encode()
        tables = parse_schema_file("list.json", content)
        assert len(tables) == 1
        assert tables[0].name == "t1"

    def test_json_empty_tables(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        content = b'{"tables": []}'
        tables = parse_schema_file("empty.json", content)
        assert tables == []

    def test_json_invalid_structure(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        content = b'"just a string"'
        with pytest.raises(ValueError, match="JSON must be"):
            parse_schema_file("bad.json", content)


class TestExcelParser:

    def test_parse_positive_xlsx(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        content = (SAMPLE_DIR / "positive_match.xlsx").read_bytes()
        tables = parse_schema_file("positive_match.xlsx", content)
        assert len(tables) == 22

    def test_parse_negative_xlsx(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        content = (SAMPLE_DIR / "negative_discrepancies.xlsx").read_bytes()
        tables = parse_schema_file("negative_discrepancies.xlsx", content)
        table_names = {t.name for t in tables}
        assert "AUDIT-LOG-FILE" in table_names
        assert "DALYREJS-FILE" not in table_names

    def test_xlsx_column_data(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        content = (SAMPLE_DIR / "positive_match.xlsx").read_bytes()
        tables = parse_schema_file("positive_match.xlsx", content)
        by_name = {t.name: t for t in tables}
        assert len(by_name["ACCOUNT-FILE"].columns) == 3


class TestParserEdgeCases:

    def test_unsupported_extension(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        with pytest.raises(ValueError, match="Unsupported file format"):
            parse_schema_file("data.pdf", b"some content")

    def test_unsupported_extension_txt(self):
        from trustbot.tools.db_schema_tool import parse_schema_file
        with pytest.raises(ValueError, match="Unsupported file format: .txt"):
            parse_schema_file("schema.txt", b"table_name,column_name\n")


# ═══════════════════════════════════════════════════════════════════════════
# 3. Schema comparator tests
# ═══════════════════════════════════════════════════════════════════════════


def _build_neo4j_entities_from_positive_csv():
    """Build Neo4j-equivalent entities from the positive CSV (simulating Neo4j data)."""
    from trustbot.models.db_entity import Neo4jDatabaseEntity, Neo4jDatabaseField
    from trustbot.tools.db_schema_tool import parse_schema_file

    content = (SAMPLE_DIR / "positive_match.csv").read_bytes()
    tables = parse_schema_file("positive_match.csv", content)
    entities = []
    for t in tables:
        fields = [
            Neo4jDatabaseField(name=c.name, data_type=c.data_type)
            for c in t.columns
        ]
        entities.append(Neo4jDatabaseEntity(
            name=t.name,
            project_id=976,
            run_id=2416,
            fields=fields,
        ))
    return entities


class TestSchemaComparator:

    def test_perfect_match(self):
        """Positive CSV vs identical Neo4j data = all MATCHED."""
        from trustbot.services.schema_comparator import compare_schemas
        from trustbot.tools.db_schema_tool import parse_schema_file

        db_tables = parse_schema_file(
            "positive_match.csv",
            (SAMPLE_DIR / "positive_match.csv").read_bytes(),
        )
        neo4j_entities = _build_neo4j_entities_from_positive_csv()

        summary = compare_schemas(db_tables, neo4j_entities)

        assert summary.total_tables == 22
        assert summary.matched_tables == 22
        assert summary.only_in_db == 0
        assert summary.only_in_neo4j == 0
        assert summary.type_mismatches == 0
        for r in summary.results:
            assert r.status == "MATCHED"
            for d in r.column_discrepancies:
                assert d.status == "MATCHED"

    def test_negative_discrepancies(self):
        """Negative CSV vs positive Neo4j data = various discrepancies."""
        from trustbot.services.schema_comparator import compare_schemas
        from trustbot.tools.db_schema_tool import parse_schema_file

        db_tables = parse_schema_file(
            "negative_discrepancies.csv",
            (SAMPLE_DIR / "negative_discrepancies.csv").read_bytes(),
        )
        neo4j_entities = _build_neo4j_entities_from_positive_csv()

        summary = compare_schemas(db_tables, neo4j_entities)

        assert summary.total_tables == 24  # 22 original - 2 removed + 2 added + 2 neo4j-only

        by_status = {}
        for r in summary.results:
            by_status.setdefault(r.status, []).append(r)

        assert len(by_status.get("ONLY_IN_DB", [])) == 2
        only_db_names = {r.table_name for r in by_status["ONLY_IN_DB"]}
        assert "AUDIT-LOG-FILE" in only_db_names
        assert "TEMP-BATCH-FILE" in only_db_names

        assert len(by_status.get("ONLY_IN_NEO4J", [])) == 2
        only_neo4j_names = {r.table_name for r in by_status["ONLY_IN_NEO4J"]}
        assert "DALYREJS-FILE" in only_neo4j_names
        assert "HTML-FILE" in only_neo4j_names

    def test_negative_type_mismatch(self):
        """Verify TYPE_MISMATCH is detected for ACCOUNT-FILE.FD-ACCT-ID."""
        from trustbot.services.schema_comparator import compare_schemas
        from trustbot.tools.db_schema_tool import parse_schema_file

        db_tables = parse_schema_file(
            "negative_discrepancies.csv",
            (SAMPLE_DIR / "negative_discrepancies.csv").read_bytes(),
        )
        neo4j_entities = _build_neo4j_entities_from_positive_csv()
        summary = compare_schemas(db_tables, neo4j_entities)

        acct_result = next(
            r for r in summary.results
            if r.table_name.upper() == "ACCOUNT-FILE" and r.status == "MATCHED"
        )
        type_mismatches = [
            d for d in acct_result.column_discrepancies
            if d.status == "TYPE_MISMATCH"
        ]
        assert len(type_mismatches) == 1
        assert type_mismatches[0].column_name == "FD-ACCT-ID"
        assert type_mismatches[0].db_type == "NUMERIC(11)"
        assert type_mismatches[0].neo4j_type == "NA"

    def test_negative_extra_column_in_db(self):
        """Verify ONLY_IN_DB column detected for CARD-FILE.FD-CARD-EXPIRY."""
        from trustbot.services.schema_comparator import compare_schemas
        from trustbot.tools.db_schema_tool import parse_schema_file

        db_tables = parse_schema_file(
            "negative_discrepancies.csv",
            (SAMPLE_DIR / "negative_discrepancies.csv").read_bytes(),
        )
        neo4j_entities = _build_neo4j_entities_from_positive_csv()
        summary = compare_schemas(db_tables, neo4j_entities)

        card_result = next(
            r for r in summary.results
            if r.table_name.upper() == "CARD-FILE" and r.status == "MATCHED"
        )
        db_only_cols = [
            d for d in card_result.column_discrepancies
            if d.status == "ONLY_IN_DB"
        ]
        assert len(db_only_cols) == 1
        assert db_only_cols[0].column_name == "FD-CARD-EXPIRY"

    def test_negative_missing_column_in_db(self):
        """Verify ONLY_IN_NEO4J column detected for XREF-FILE.FD-XREF-FILLER."""
        from trustbot.services.schema_comparator import compare_schemas
        from trustbot.tools.db_schema_tool import parse_schema_file

        db_tables = parse_schema_file(
            "negative_discrepancies.csv",
            (SAMPLE_DIR / "negative_discrepancies.csv").read_bytes(),
        )
        neo4j_entities = _build_neo4j_entities_from_positive_csv()
        summary = compare_schemas(db_tables, neo4j_entities)

        xref_result = next(
            r for r in summary.results
            if r.table_name.upper() == "XREF-FILE" and r.status == "MATCHED"
        )
        neo4j_only_cols = [
            d for d in xref_result.column_discrepancies
            if d.status == "ONLY_IN_NEO4J"
        ]
        assert len(neo4j_only_cols) == 1
        assert neo4j_only_cols[0].column_name == "FD-XREF-FILLER"

    def test_both_empty(self):
        """Empty DB + empty Neo4j = 0 tables, no discrepancies."""
        from trustbot.services.schema_comparator import compare_schemas
        summary = compare_schemas([], [])
        assert summary.total_tables == 0
        assert summary.results == []

    def test_db_only(self):
        """DB has tables, Neo4j is empty = all ONLY_IN_DB."""
        from trustbot.models.db_entity import DatabaseColumn, DatabaseTable
        from trustbot.services.schema_comparator import compare_schemas

        db_tables = [
            DatabaseTable(name="T1", columns=[DatabaseColumn(name="c1")]),
            DatabaseTable(name="T2", columns=[]),
        ]
        summary = compare_schemas(db_tables, [])
        assert summary.total_tables == 2
        assert summary.only_in_db == 2
        assert summary.matched_tables == 0

    def test_neo4j_only(self):
        """Neo4j has entities, DB is empty = all ONLY_IN_NEO4J."""
        from trustbot.models.db_entity import Neo4jDatabaseEntity, Neo4jDatabaseField
        from trustbot.services.schema_comparator import compare_schemas

        neo4j = [
            Neo4jDatabaseEntity(name="E1", fields=[Neo4jDatabaseField(name="f1")]),
        ]
        summary = compare_schemas([], neo4j)
        assert summary.total_tables == 1
        assert summary.only_in_neo4j == 1
        assert summary.matched_tables == 0

    def test_case_insensitive_matching(self):
        """Table names differing only in case should match."""
        from trustbot.models.db_entity import (
            DatabaseColumn, DatabaseTable,
            Neo4jDatabaseEntity, Neo4jDatabaseField,
        )
        from trustbot.services.schema_comparator import compare_schemas

        db_tables = [DatabaseTable(
            name="Users",
            columns=[DatabaseColumn(name="ID", data_type="int")],
        )]
        neo4j = [Neo4jDatabaseEntity(
            name="users",
            fields=[Neo4jDatabaseField(name="id", data_type="int")],
        )]
        summary = compare_schemas(db_tables, neo4j)
        assert summary.matched_tables == 1
        assert summary.only_in_db == 0
        assert summary.only_in_neo4j == 0

    def test_summary_column_counts(self):
        """Verify column-level aggregate counts in the summary."""
        from trustbot.services.schema_comparator import compare_schemas
        from trustbot.tools.db_schema_tool import parse_schema_file

        db_tables = parse_schema_file(
            "negative_discrepancies.csv",
            (SAMPLE_DIR / "negative_discrepancies.csv").read_bytes(),
        )
        neo4j_entities = _build_neo4j_entities_from_positive_csv()
        summary = compare_schemas(db_tables, neo4j_entities)

        assert summary.type_mismatches >= 1
        assert summary.columns_only_in_db >= 1
        assert summary.columns_only_in_neo4j >= 1
        assert summary.matched_columns > 0


# ═══════════════════════════════════════════════════════════════════════════
# 4. Neo4j entity tool deduplication tests (mocked driver)
# ═══════════════════════════════════════════════════════════════════════════


class _FakeNode(dict):
    """Mimics a neo4j.graph.Node with dict-like access and element_id."""
    @property
    def element_id(self):
        return "fake:0"


def _make_entity_node(name, project_id=976, run_id=2416, schema_table="Public"):
    node = _FakeNode(
        name=name,
        project_id=project_id,
        run_id=run_id,
        schema_table=schema_table,
    )
    return node


def _make_field_node(name, data_type="NA", constraints=None):
    node = _FakeNode(
        name=name,
        data_type=data_type,
        constraints=constraints or [],
    )
    return node


class _FakeRecord:
    def __init__(self, entity_node, field_nodes):
        self._data = {"e": entity_node, "fields": field_nodes}

    def __getitem__(self, key):
        return self._data[key]


class _FakeResult:
    def __init__(self, records):
        self._records = records
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._records):
            raise StopAsyncIteration
        rec = self._records[self._idx]
        self._idx += 1
        return rec


@pytest.mark.asyncio
async def test_neo4j_entity_deduplication():
    """Same entity name from multiple files should be deduplicated, fields merged."""
    from trustbot.tools.neo4j_entity_tool import fetch_database_entities

    ent_node = _make_entity_node("ACCOUNT-FILE")
    records = [
        _FakeRecord(ent_node, [
            _make_field_node("FD-ACCT-ID"),
            _make_field_node("FD-ACCT-DATA"),
        ]),
        _FakeRecord(ent_node, [
            _make_field_node("FD-ACCT-ID"),
            _make_field_node("FD-ACCTFILE-REC"),
        ]),
        _FakeRecord(ent_node, [
            _make_field_node("FD-ACCT-DATA"),
            _make_field_node("FD-ACCTFILE-REC"),
        ]),
    ]

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=_FakeResult(records))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=mock_session)

    entities = await fetch_database_entities(mock_driver, 976, 2416)

    assert len(entities) == 1
    assert entities[0].name == "ACCOUNT-FILE"
    field_names = {f.name for f in entities[0].fields}
    assert field_names == {"FD-ACCT-ID", "FD-ACCT-DATA", "FD-ACCTFILE-REC"}


@pytest.mark.asyncio
async def test_neo4j_entity_schema_table_fallback():
    """schema_name should fall back to schema_table property."""
    from trustbot.tools.neo4j_entity_tool import fetch_database_entities

    ent_node = _FakeNode(
        name="TEST-TABLE",
        project_id=1,
        run_id=1,
        schema_table="MySchema",
    )
    records = [_FakeRecord(ent_node, [])]

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=_FakeResult(records))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=mock_session)

    entities = await fetch_database_entities(mock_driver, 1, 1)

    assert len(entities) == 1
    assert entities[0].schema_name == "MySchema"


@pytest.mark.asyncio
async def test_neo4j_entity_constraints_parsing():
    """is_nullable and is_primary_key should be derived from constraints list."""
    from trustbot.tools.neo4j_entity_tool import fetch_database_entities

    ent_node = _make_entity_node("TEST")
    records = [_FakeRecord(ent_node, [
        _make_field_node("pk_col", constraints=["PRIMARY KEY", "NOT NULL"]),
        _make_field_node("nullable_col", constraints=[]),
    ])]

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=_FakeResult(records))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=mock_session)

    entities = await fetch_database_entities(mock_driver, 976, 2416)

    fields_by_name = {f.name: f for f in entities[0].fields}
    assert fields_by_name["pk_col"].is_primary_key is True
    assert fields_by_name["pk_col"].is_nullable is False
    assert fields_by_name["nullable_col"].is_primary_key is False
    assert fields_by_name["nullable_col"].is_nullable is True


@pytest.mark.asyncio
async def test_neo4j_entity_empty_result():
    """No entities found should return empty list."""
    from trustbot.tools.neo4j_entity_tool import fetch_database_entities

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=_FakeResult([]))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=mock_session)

    entities = await fetch_database_entities(mock_driver, 999, 999)
    assert entities == []


# ═══════════════════════════════════════════════════════════════════════════
# 5. Integration: sample files + comparator end-to-end
# ═══════════════════════════════════════════════════════════════════════════


class TestIntegrationCSV:
    """Run the full pipeline: parse CSV -> compare against mock Neo4j entities."""

    def test_positive_csv_full_match(self):
        from trustbot.services.schema_comparator import compare_schemas
        from trustbot.tools.db_schema_tool import parse_schema_file

        db_tables = parse_schema_file(
            "positive_match.csv",
            (SAMPLE_DIR / "positive_match.csv").read_bytes(),
        )
        neo4j_entities = _build_neo4j_entities_from_positive_csv()
        summary = compare_schemas(db_tables, neo4j_entities)

        assert summary.matched_tables == 22
        assert summary.only_in_db == 0
        assert summary.only_in_neo4j == 0
        assert summary.type_mismatches == 0
        assert summary.columns_only_in_db == 0
        assert summary.columns_only_in_neo4j == 0

    def test_negative_csv_discrepancies(self):
        from trustbot.services.schema_comparator import compare_schemas
        from trustbot.tools.db_schema_tool import parse_schema_file

        db_tables = parse_schema_file(
            "negative_discrepancies.csv",
            (SAMPLE_DIR / "negative_discrepancies.csv").read_bytes(),
        )
        neo4j_entities = _build_neo4j_entities_from_positive_csv()
        summary = compare_schemas(db_tables, neo4j_entities)

        assert summary.only_in_db == 2
        assert summary.only_in_neo4j == 2
        assert summary.type_mismatches >= 1
        assert summary.columns_only_in_db >= 1
        assert summary.columns_only_in_neo4j >= 1


class TestIntegrationJSON:

    def test_positive_json_full_match(self):
        from trustbot.services.schema_comparator import compare_schemas
        from trustbot.tools.db_schema_tool import parse_schema_file

        db_tables = parse_schema_file(
            "positive_match.json",
            (SAMPLE_DIR / "positive_match.json").read_bytes(),
        )
        neo4j_entities = _build_neo4j_entities_from_positive_csv()
        summary = compare_schemas(db_tables, neo4j_entities)

        assert summary.matched_tables == 22
        assert summary.only_in_db == 0
        assert summary.only_in_neo4j == 0

    def test_negative_json_discrepancies(self):
        from trustbot.services.schema_comparator import compare_schemas
        from trustbot.tools.db_schema_tool import parse_schema_file

        db_tables = parse_schema_file(
            "negative_discrepancies.json",
            (SAMPLE_DIR / "negative_discrepancies.json").read_bytes(),
        )
        neo4j_entities = _build_neo4j_entities_from_positive_csv()
        summary = compare_schemas(db_tables, neo4j_entities)

        assert summary.only_in_db == 2
        assert summary.only_in_neo4j == 2


class TestIntegrationExcel:

    def test_positive_xlsx_full_match(self):
        from trustbot.services.schema_comparator import compare_schemas
        from trustbot.tools.db_schema_tool import parse_schema_file

        db_tables = parse_schema_file(
            "positive_match.xlsx",
            (SAMPLE_DIR / "positive_match.xlsx").read_bytes(),
        )
        neo4j_entities = _build_neo4j_entities_from_positive_csv()
        summary = compare_schemas(db_tables, neo4j_entities)

        assert summary.matched_tables == 22
        assert summary.only_in_db == 0
        assert summary.only_in_neo4j == 0

    def test_negative_xlsx_discrepancies(self):
        from trustbot.services.schema_comparator import compare_schemas
        from trustbot.tools.db_schema_tool import parse_schema_file

        db_tables = parse_schema_file(
            "negative_discrepancies.xlsx",
            (SAMPLE_DIR / "negative_discrepancies.xlsx").read_bytes(),
        )
        neo4j_entities = _build_neo4j_entities_from_positive_csv()
        summary = compare_schemas(db_tables, neo4j_entities)

        assert summary.only_in_db == 2
        assert summary.only_in_neo4j == 2


class TestCrossFormatConsistency:
    """All three formats should produce identical results when fed the same data."""

    def test_positive_all_formats_match(self):
        from trustbot.tools.db_schema_tool import parse_schema_file

        csv_tables = parse_schema_file(
            "p.csv", (SAMPLE_DIR / "positive_match.csv").read_bytes(),
        )
        json_tables = parse_schema_file(
            "p.json", (SAMPLE_DIR / "positive_match.json").read_bytes(),
        )
        xlsx_tables = parse_schema_file(
            "p.xlsx", (SAMPLE_DIR / "positive_match.xlsx").read_bytes(),
        )

        csv_names = sorted(t.name for t in csv_tables)
        json_names = sorted(t.name for t in json_tables)
        xlsx_names = sorted(t.name for t in xlsx_tables)

        assert csv_names == json_names == xlsx_names

        csv_by_name = {t.name: t for t in csv_tables}
        json_by_name = {t.name: t for t in json_tables}
        xlsx_by_name = {t.name: t for t in xlsx_tables}

        for name in csv_names:
            csv_cols = sorted(c.name for c in csv_by_name[name].columns)
            json_cols = sorted(c.name for c in json_by_name[name].columns)
            xlsx_cols = sorted(c.name for c in xlsx_by_name[name].columns)
            assert csv_cols == json_cols == xlsx_cols, (
                f"Column mismatch for {name}: CSV={csv_cols} JSON={json_cols} XLSX={xlsx_cols}"
            )

    def test_negative_all_formats_match(self):
        from trustbot.services.schema_comparator import compare_schemas
        from trustbot.tools.db_schema_tool import parse_schema_file

        neo4j_entities = _build_neo4j_entities_from_positive_csv()

        results = {}
        for fmt, filename in [
            ("csv", "negative_discrepancies.csv"),
            ("json", "negative_discrepancies.json"),
            ("xlsx", "negative_discrepancies.xlsx"),
        ]:
            tables = parse_schema_file(
                filename, (SAMPLE_DIR / filename).read_bytes(),
            )
            summary = compare_schemas(tables, neo4j_entities)
            results[fmt] = summary

        for fmt in ("json", "xlsx"):
            assert results[fmt].total_tables == results["csv"].total_tables, (
                f"{fmt} total_tables mismatch"
            )
            assert results[fmt].matched_tables == results["csv"].matched_tables, (
                f"{fmt} matched_tables mismatch"
            )
            assert results[fmt].only_in_db == results["csv"].only_in_db, (
                f"{fmt} only_in_db mismatch"
            )
            assert results[fmt].only_in_neo4j == results["csv"].only_in_neo4j, (
                f"{fmt} only_in_neo4j mismatch"
            )
