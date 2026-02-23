"""Compare database schema (from DB or flat file) against Neo4j DatabaseEntity nodes."""

from __future__ import annotations

import logging

from trustbot.models.db_entity import (
    ColumnDiscrepancy,
    DatabaseColumn,
    DatabaseTable,
    EntityComparisonResult,
    Neo4jDatabaseEntity,
    Neo4jDatabaseField,
    SchemaComparisonSummary,
)

logger = logging.getLogger("trustbot.services.schema_comparator")


def compare_schemas(
    db_tables: list[DatabaseTable],
    neo4j_entities: list[Neo4jDatabaseEntity],
) -> SchemaComparisonSummary:
    """
    Compare DB tables against Neo4j DatabaseEntity nodes.

    Step 1: Table-level matching (case-insensitive by name).
    Step 2: For matched tables, column-level comparison.
    """
    db_map: dict[str, DatabaseTable] = {
        t.name.lower(): t for t in db_tables
    }
    neo4j_map: dict[str, Neo4jDatabaseEntity] = {
        e.name.lower(): e for e in neo4j_entities
    }

    all_keys = sorted(set(db_map.keys()) | set(neo4j_map.keys()))
    results: list[EntityComparisonResult] = []

    total_cols_compared = 0
    matched_cols = 0
    cols_only_db = 0
    cols_only_neo4j = 0
    type_mismatches = 0

    for key in all_keys:
        db_table = db_map.get(key)
        neo4j_entity = neo4j_map.get(key)

        display_name = (
            db_table.name if db_table
            else neo4j_entity.name if neo4j_entity
            else key
        )

        if db_table and neo4j_entity:
            discrepancies = _compare_columns(db_table.columns, neo4j_entity.fields)
            results.append(EntityComparisonResult(
                table_name=display_name,
                status="MATCHED",
                db_columns=db_table.columns,
                neo4j_fields=neo4j_entity.fields,
                column_discrepancies=discrepancies,
            ))
            for d in discrepancies:
                total_cols_compared += 1
                if d.status == "MATCHED":
                    matched_cols += 1
                elif d.status == "ONLY_IN_DB":
                    cols_only_db += 1
                elif d.status == "ONLY_IN_NEO4J":
                    cols_only_neo4j += 1
                elif d.status == "TYPE_MISMATCH":
                    type_mismatches += 1

        elif db_table:
            results.append(EntityComparisonResult(
                table_name=display_name,
                status="ONLY_IN_DB",
                db_columns=db_table.columns,
            ))

        else:
            results.append(EntityComparisonResult(
                table_name=display_name,
                status="ONLY_IN_NEO4J",
                neo4j_fields=neo4j_entity.fields,
            ))

    matched_tables = sum(1 for r in results if r.status == "MATCHED")
    only_in_db = sum(1 for r in results if r.status == "ONLY_IN_DB")
    only_in_neo4j = sum(1 for r in results if r.status == "ONLY_IN_NEO4J")

    summary = SchemaComparisonSummary(
        total_tables=len(results),
        matched_tables=matched_tables,
        only_in_db=only_in_db,
        only_in_neo4j=only_in_neo4j,
        total_columns_compared=total_cols_compared,
        matched_columns=matched_cols,
        columns_only_in_db=cols_only_db,
        columns_only_in_neo4j=cols_only_neo4j,
        type_mismatches=type_mismatches,
        results=results,
    )

    logger.info(
        "Schema comparison: %d tables (%d matched, %d DB-only, %d Neo4j-only), "
        "%d columns compared (%d matched, %d DB-only, %d Neo4j-only, %d type mismatches)",
        summary.total_tables, matched_tables, only_in_db, only_in_neo4j,
        total_cols_compared, matched_cols, cols_only_db, cols_only_neo4j, type_mismatches,
    )
    return summary


def _compare_columns(
    db_columns: list[DatabaseColumn],
    neo4j_fields: list[Neo4jDatabaseField],
) -> list[ColumnDiscrepancy]:
    """Compare columns of a matched table. Case-insensitive name matching."""

    db_col_map: dict[str, DatabaseColumn] = {
        c.name.lower(): c for c in db_columns
    }
    neo4j_col_map: dict[str, Neo4jDatabaseField] = {
        f.name.lower(): f for f in neo4j_fields
    }

    all_col_keys = sorted(set(db_col_map.keys()) | set(neo4j_col_map.keys()))
    discrepancies: list[ColumnDiscrepancy] = []

    for col_key in all_col_keys:
        db_col = db_col_map.get(col_key)
        neo4j_col = neo4j_col_map.get(col_key)

        display_name = (
            db_col.name if db_col
            else neo4j_col.name if neo4j_col
            else col_key
        )

        if db_col and neo4j_col:
            db_type_norm = db_col.data_type.strip().lower()
            neo4j_type_norm = neo4j_col.data_type.strip().lower()

            if db_type_norm and neo4j_type_norm and db_type_norm != neo4j_type_norm:
                discrepancies.append(ColumnDiscrepancy(
                    column_name=display_name,
                    status="TYPE_MISMATCH",
                    db_type=db_col.data_type,
                    neo4j_type=neo4j_col.data_type,
                ))
            else:
                discrepancies.append(ColumnDiscrepancy(
                    column_name=display_name,
                    status="MATCHED",
                    db_type=db_col.data_type,
                    neo4j_type=neo4j_col.data_type,
                ))

        elif db_col:
            discrepancies.append(ColumnDiscrepancy(
                column_name=display_name,
                status="ONLY_IN_DB",
                db_type=db_col.data_type,
            ))

        else:
            discrepancies.append(ColumnDiscrepancy(
                column_name=display_name,
                status="ONLY_IN_NEO4J",
                neo4j_type=neo4j_col.data_type,
            ))

    return discrepancies
