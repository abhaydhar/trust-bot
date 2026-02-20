"""
SQLite-based Code Index for function name → file path lookups.

Maps every function/program name in the repo to its file path.
Used by Agent 2 for fast callee resolution without filesystem scans.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from trustbot.config import settings
from trustbot.indexing.chunker import chunk_file, LANGUAGE_MAP
from trustbot.tools.filesystem_tool import CODE_EXTENSIONS, IGNORED_DIRS

logger = logging.getLogger("trustbot.index")


class CodeIndex:
    """
    Pre-built lookup table mapping function names to file paths.
    MVP implementation using SQLite.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or (settings.codebase_root / ".trustbot_code_index.db")
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS code_index (
                function_name TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                language TEXT NOT NULL,
                class_name TEXT,
                last_indexed TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_function_name ON code_index(function_name)"
        )
        conn.commit()

    def build(self, codebase_root: Path | None = None) -> dict:
        """
        Build or rebuild the code index by scanning the codebase.
        Returns stats: {functions, files, duration_seconds}.
        """
        root = codebase_root or settings.codebase_root.resolve()
        if not root.exists():
            raise FileNotFoundError(f"Codebase root does not exist: {root}")

        conn = self._get_conn()
        conn.execute("DELETE FROM code_index")
        conn.commit()

        start = datetime.utcnow()
        total_functions = 0
        total_files = 0

        for root_dir, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for filename in files:
                ext = Path(filename).suffix
                if ext not in CODE_EXTENSIONS:
                    continue

                filepath = Path(root_dir) / filename
                try:
                    rel_path = str(filepath.relative_to(root))
                except ValueError:
                    continue

                try:
                    chunks = chunk_file(filepath, root)
                except Exception as e:
                    logger.debug("Skipping %s: %s", filepath, e)
                    continue

                lang = LANGUAGE_MAP.get(ext, "unknown")
                for chunk in chunks:
                    name = chunk.function_name or chunk.class_name
                    if not name:
                        continue
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO code_index
                        (function_name, file_path, language, class_name, last_indexed)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (name, rel_path, lang, chunk.class_name or "", start.isoformat()),
                    )
                    total_functions += 1
                total_files += 1

        conn.commit()
        duration = (datetime.utcnow() - start).total_seconds()

        logger.info(
            "Code index built: %d functions from %d files in %.1fs",
            total_functions, total_files, duration,
        )
        return {
            "functions": total_functions,
            "files": total_files,
            "duration_seconds": duration,
        }

    def lookup(self, function_name: str) -> str | None:
        """
        Resolve a function name to its file path.
        Returns None if not found.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT file_path FROM code_index WHERE function_name = ?",
            (function_name.strip(),),
        ).fetchone()
        if row:
            return row["file_path"]
        # Try case-insensitive
        row = conn.execute(
            "SELECT file_path FROM code_index WHERE LOWER(function_name) = LOWER(?)",
            (function_name.strip(),),
        ).fetchone()
        return row["file_path"] if row else None

    def lookup_all(self, function_names: list[str]) -> dict[str, str | None]:
        """Batch lookup. Returns dict of name -> file_path (or None)."""
        return {name: self.lookup(name) for name in function_names}

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
