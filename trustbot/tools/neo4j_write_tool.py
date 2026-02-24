"""Dedicated Neo4j write tool for topic updates only.

Separated from the read-only Neo4jTool to maintain strict guardrails.
Only SET n.topic operations are permitted; all other writes are rejected.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from datetime import datetime
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncDriver
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError

from trustbot.config import settings
from trustbot.models.topic_convergence import TopicChangeRecord
from trustbot.tools.base import BaseTool

logger = logging.getLogger("trustbot.tools.neo4j_write")

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.5

ALLOWED_LABELS = frozenset({
    "Snippet", "DBCall", "Calculation", "ServiceCall",
    "InputEntity", "InputInterface", "Variable",
    "Job", "Step", "JclJob",
    "DatabaseEntity", "DatabaseField",
})


class Neo4jWriteTool(BaseTool):
    """Write-only Neo4j tool scoped exclusively to topic field updates."""

    name = "neo4j_write"
    description = "Update topic fields on Neo4j nodes (topic-only writes with audit log)."

    def __init__(self) -> None:
        super().__init__()
        self._driver: AsyncDriver | None = None
        self._change_log: list[TopicChangeRecord] = []

    async def initialize(self) -> None:
        self._driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            max_connection_lifetime=settings.neo4j_max_connection_lifetime,
            max_connection_pool_size=settings.neo4j_max_connection_pool_size,
            connection_acquisition_timeout=settings.neo4j_connection_acquisition_timeout,
            connection_timeout=settings.neo4j_connection_timeout,
            max_transaction_retry_time=settings.neo4j_max_transaction_retry_time,
            keep_alive=settings.neo4j_keep_alive,
        )
        await self._driver.verify_connectivity()
        logger.info("Neo4jWriteTool connected to %s", settings.neo4j_uri)

    async def shutdown(self) -> None:
        if self._driver:
            await self._driver.close()
            self._driver = None

    @property
    def driver(self) -> AsyncDriver:
        if self._driver is None:
            raise RuntimeError("Neo4jWriteTool driver not initialized.")
        return self._driver

    @property
    def change_log(self) -> list[TopicChangeRecord]:
        return list(self._change_log)

    # ------------------------------------------------------------------
    # Internal retry helper
    # ------------------------------------------------------------------

    async def _run_with_retry(self, coro_factory, description: str = "write"):
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except (ServiceUnavailable, SessionExpired, TransientError, OSError) as exc:
                last_error = exc
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF_BASE ** attempt
                    logger.warning(
                        "Neo4jWriteTool %s failed (attempt %d/%d): %s. Retrying in %.1fs...",
                        description, attempt, MAX_RETRIES, exc, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "Neo4jWriteTool %s failed after %d attempts: %s",
                        description, MAX_RETRIES, exc,
                    )
        raise last_error  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _validate_label(self, label: str) -> None:
        if label not in ALLOWED_LABELS:
            raise PermissionError(
                f"Label '{label}' is not in the allowed set: {sorted(ALLOWED_LABELS)}"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def update_node_topic(
        self,
        node_key: str,
        node_label: str,
        new_topic: str,
        *,
        execution_flow_key: str = "",
        changed_by: str = "user",
    ) -> TopicChangeRecord:
        """Update the topic field on a single node. Returns the change record."""
        self._validate_label(node_label)

        cypher = (
            f"MATCH (n:{node_label} {{key: $key}}) "
            "RETURN n.topic AS old_topic, n.key AS key"
        )

        async def _read_old():
            async with self.driver.session() as session:
                result = await session.run(cypher, key=node_key)
                record = await result.single()
            if record is None:
                raise ValueError(
                    f"No {node_label} node found with key '{node_key}'"
                )
            return record["old_topic"] or ""

        old_topic = await self._run_with_retry(_read_old, f"read_old({node_key})")

        write_cypher = (
            f"MATCH (n:{node_label} {{key: $key}}) "
            "SET n.topic = $new_topic "
            "RETURN n.key AS key"
        )

        async def _write():
            async with self.driver.session() as session:
                result = await session.run(write_cypher, key=node_key, new_topic=new_topic)
                record = await result.single()
            if record is None:
                raise RuntimeError(f"Write failed for {node_label} key={node_key}")
            return record["key"]

        await self._run_with_retry(_write, f"update_topic({node_key})")

        change = TopicChangeRecord(
            node_key=node_key,
            node_type=node_label,
            node_label=node_label,
            old_topic=old_topic,
            new_topic=new_topic,
            changed_by=changed_by,
            changed_at=datetime.utcnow(),
            execution_flow_key=execution_flow_key,
        )
        self._change_log.append(change)
        logger.info("Updated topic on %s/%s: '%s' -> '%s'", node_label, node_key, old_topic, new_topic)
        return change

    async def bulk_update_topics(
        self,
        updates: list[dict[str, str]],
        *,
        execution_flow_key: str = "",
        changed_by: str = "bulk",
    ) -> list[TopicChangeRecord]:
        """Batch-update topics. Each dict needs: key, label, new_topic."""
        records: list[TopicChangeRecord] = []
        for item in updates:
            node_key = item["key"]
            node_label = item["label"]
            new_topic = item["new_topic"]
            rec = await self.update_node_topic(
                node_key, node_label, new_topic,
                execution_flow_key=execution_flow_key,
                changed_by=changed_by,
            )
            records.append(rec)
        return records

    async def restore_topic(
        self,
        node_key: str,
        node_label: str,
        original_topic: str,
        *,
        execution_flow_key: str = "",
    ) -> TopicChangeRecord:
        """Undo a previous topic change by restoring the original value."""
        self._validate_label(node_label)

        write_cypher = (
            f"MATCH (n:{node_label} {{key: $key}}) "
            "SET n.topic = $topic "
            "RETURN n.topic AS restored_topic"
        )

        async def _restore():
            async with self.driver.session() as session:
                result = await session.run(write_cypher, key=node_key, topic=original_topic)
                record = await result.single()
            if record is None:
                raise ValueError(f"Restore failed for {node_label} key={node_key}")
            return record["restored_topic"]

        await self._run_with_retry(_restore, f"restore_topic({node_key})")

        change = TopicChangeRecord(
            node_key=node_key,
            node_type=node_label,
            node_label=node_label,
            old_topic="(reverted)",
            new_topic=original_topic,
            changed_by="undo",
            changed_at=datetime.utcnow(),
            execution_flow_key=execution_flow_key,
            is_undo=True,
        )
        self._change_log.append(change)
        logger.info("Restored topic on %s/%s to '%s'", node_label, node_key, original_topic)
        return change

    # ------------------------------------------------------------------
    # Audit log helpers
    # ------------------------------------------------------------------

    def export_audit_json(self) -> str:
        return json.dumps(
            [r.model_dump(mode="json") for r in self._change_log],
            indent=2,
            default=str,
        )

    def export_audit_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=[
                "node_key", "node_type", "node_label",
                "old_topic", "new_topic", "changed_by",
                "changed_at", "execution_flow_key", "is_undo",
            ],
        )
        writer.writeheader()
        for rec in self._change_log:
            writer.writerow(rec.model_dump(mode="json"))
        return buf.getvalue()

    def clear_audit_log(self) -> None:
        self._change_log.clear()
