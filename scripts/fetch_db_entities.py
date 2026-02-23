"""Fetch DatabaseEntity + DatabaseField nodes from Neo4j for a given project_id."""

import asyncio
import json
from neo4j import AsyncGraphDatabase


async def main():
    uri = "bolt://rapidx-neo4j-dev.southindia.cloudapp.azure.com:7687/neo4j"
    user = "neo4j"
    password = "Rapidxneo4jdev"
    project_id = 976

    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    await driver.verify_connectivity()
    print(f"Connected to Neo4j at {uri}")

    # First, discover what run_ids exist for this project
    run_query = """
    MATCH (e:DatabaseEntity {project_id: $pid})
    RETURN DISTINCT e.run_id AS run_id
    ORDER BY e.run_id
    """
    async with driver.session() as session:
        result = await session.run(run_query, pid=project_id)
        run_ids = [record["run_id"] async for record in result]
    print(f"Run IDs for project_id={project_id}: {run_ids}")

    # Fetch all entities with fields
    entity_query = """
    MATCH (e:DatabaseEntity {project_id: $pid})
    OPTIONAL MATCH (e)-[:HAS_FIELD]->(f:DatabaseField)
    RETURN e, collect(f) as fields
    ORDER BY e.run_id, e.name
    """

    entities = []
    async with driver.session() as session:
        result = await session.run(entity_query, pid=project_id)
        async for record in result:
            e = record["e"]
            fields_raw = record["fields"]

            entity_props = dict(e)
            fields = []
            for f in fields_raw:
                if f is not None:
                    fields.append(dict(f))

            entities.append({
                "entity": entity_props,
                "fields": fields,
            })

    print(f"\nTotal DatabaseEntity nodes: {len(entities)}")

    # Print summary
    by_run = {}
    for ent in entities:
        rid = ent["entity"].get("run_id", "?")
        by_run.setdefault(rid, []).append(ent)

    for rid, ents in sorted(by_run.items(), key=lambda x: str(x[0])):
        print(f"\n  run_id={rid}: {len(ents)} entities")
        for ent in ents[:5]:
            name = ent["entity"].get("name", "?")
            schema = ent["entity"].get("schema_name", "?")
            n_fields = len(ent["fields"])
            print(f"    - {schema}.{name} ({n_fields} fields)")
        if len(ents) > 5:
            print(f"    ... and {len(ents) - 5} more")

    # Dump full data
    with open("scripts/neo4j_db_entities_976.json", "w") as fp:
        json.dump(entities, fp, indent=2, default=str)
    print(f"\nFull data written to scripts/neo4j_db_entities_976.json")

    # Also print all property keys we see on entities and fields
    entity_keys = set()
    field_keys = set()
    for ent in entities:
        entity_keys.update(ent["entity"].keys())
        for f in ent["fields"]:
            field_keys.update(f.keys())
    print(f"\nDatabaseEntity property keys: {sorted(entity_keys)}")
    print(f"DatabaseField property keys: {sorted(field_keys)}")

    await driver.close()


if __name__ == "__main__":
    asyncio.run(main())
