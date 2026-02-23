"""Query Neo4j for DatabaseEntity and DatabaseField nodes."""

from __future__ import annotations

import logging

from neo4j import AsyncDriver

from trustbot.models.db_entity import Neo4jDatabaseEntity, Neo4jDatabaseField

logger = logging.getLogger("trustbot.tools.neo4j_entity")


async def fetch_database_entities(
    driver: AsyncDriver,
    project_id: int,
    run_id: int,
) -> list[Neo4jDatabaseEntity]:
    """
    Fetch all DatabaseEntity nodes (and their HAS_FIELD → DatabaseField children)
    for the given project_id and run_id.

    Reuses an existing Neo4j AsyncDriver (from Neo4jTool) so the connection
    pool is shared.
    """
    query = """
    MATCH (e:DatabaseEntity {project_id: $pid, run_id: $rid})
    OPTIONAL MATCH (e)-[:HAS_FIELD]->(f:DatabaseField)
    RETURN e, collect(f) as fields
    ORDER BY e.name
    """

    # Collect raw entities; same name may appear multiple times across files
    raw_entities: list[tuple] = []

    async with driver.session() as session:
        result = await session.run(query, pid=project_id, rid=run_id)
        async for record in result:
            entity_node = record["e"]
            field_nodes = record["fields"]

            fields: list[Neo4jDatabaseField] = []
            for f in field_nodes:
                if f is None:
                    continue
                constraints = f.get("constraints", []) or []
                fields.append(Neo4jDatabaseField(
                    name=f.get("name", ""),
                    data_type=f.get("data_type", ""),
                    is_nullable="NOT NULL" not in constraints,
                    is_primary_key="PRIMARY KEY" in constraints,
                ))

            schema_name = (
                entity_node.get("schema_name")
                or entity_node.get("schema_table")
                or ""
            )
            raw_entities.append((entity_node, schema_name, fields))

    # Deduplicate by entity name, merging fields from duplicate entries
    merged: dict[str, Neo4jDatabaseEntity] = {}
    for entity_node, schema_name, fields in raw_entities:
        name = entity_node.get("name", "")
        if name not in merged:
            merged[name] = Neo4jDatabaseEntity(
                name=name,
                schema_name=schema_name,
                project_id=entity_node.get("project_id"),
                run_id=entity_node.get("run_id"),
                fields=[],
            )
        seen_field_names = {f.name for f in merged[name].fields}
        for f in fields:
            if f.name not in seen_field_names:
                merged[name].fields.append(f)
                seen_field_names.add(f.name)

    entities = sorted(merged.values(), key=lambda e: e.name)

    logger.info(
        "Fetched %d raw DatabaseEntity nodes, deduplicated to %d unique entities "
        "(%d total fields) for project_id=%d, run_id=%d",
        len(raw_entities),
        len(entities),
        sum(len(e.fields) for e in entities),
        project_id,
        run_id,
    )
    return entities
