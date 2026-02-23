"""Pydantic models for database entity verification."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DatabaseColumn(BaseModel):
    """A column in a database table, sourced from the actual DB or flat file."""

    name: str
    data_type: str = ""
    is_nullable: bool = True
    is_primary_key: bool = False


class DatabaseTable(BaseModel):
    """A table in the actual database schema."""

    name: str
    schema_name: str = ""
    columns: list[DatabaseColumn] = Field(default_factory=list)


class Neo4jDatabaseField(BaseModel):
    """A DatabaseField node from Neo4j, connected to a DatabaseEntity via HAS_FIELD."""

    name: str
    data_type: str = ""
    is_nullable: bool = True
    is_primary_key: bool = False


class Neo4jDatabaseEntity(BaseModel):
    """A DatabaseEntity node from Neo4j representing a table."""

    name: str
    schema_name: str = ""
    project_id: int | None = None
    run_id: int | None = None
    fields: list[Neo4jDatabaseField] = Field(default_factory=list)


class ColumnDiscrepancy(BaseModel):
    """Column-level comparison result for a single column within a matched table."""

    column_name: str
    status: str  # MATCHED, ONLY_IN_DB, ONLY_IN_NEO4J, TYPE_MISMATCH
    db_type: str = ""
    neo4j_type: str = ""


class EntityComparisonResult(BaseModel):
    """Comparison result for a single table/entity."""

    table_name: str
    status: str  # MATCHED, ONLY_IN_DB, ONLY_IN_NEO4J
    db_columns: list[DatabaseColumn] = Field(default_factory=list)
    neo4j_fields: list[Neo4jDatabaseField] = Field(default_factory=list)
    column_discrepancies: list[ColumnDiscrepancy] = Field(default_factory=list)


class SchemaComparisonSummary(BaseModel):
    """Aggregate summary of a full schema comparison."""

    total_tables: int = 0
    matched_tables: int = 0
    only_in_db: int = 0
    only_in_neo4j: int = 0
    total_columns_compared: int = 0
    matched_columns: int = 0
    columns_only_in_db: int = 0
    columns_only_in_neo4j: int = 0
    type_mismatches: int = 0
    results: list[EntityComparisonResult] = Field(default_factory=list)
