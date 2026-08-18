"""Fresh-install schema initialization for the local user database.

This module intentionally contains no upgrade path.  Development currently
starts from an empty database; finding tables or a schema version different
from the current one is an explicit error rather than an implicit migration.
"""

from __future__ import annotations

import sys
from pathlib import Path

from db.connection import get_connection


USER_SCHEMA_VERSION = 1
_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
_EXPECTED_TABLES = {"users", "api_keys"}

sys.modules.setdefault("db.schema", sys.modules[__name__])
sys.modules.setdefault("app.db.schema", sys.modules[__name__])


class IncompatibleUserSchemaError(RuntimeError):
    """Raised when a database was not created by the current clean schema."""


def initialize_schema(db_path: str | Path) -> None:
    """Create the current schema only when the database is completely empty."""

    conn = get_connection(db_path)
    try:
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if current == USER_SCHEMA_VERSION:
            if tables != _EXPECTED_TABLES:
                raise IncompatibleUserSchemaError(
                    "User database schema is incomplete; reset the local database"
                )
            return
        if current != 0 or tables:
            raise IncompatibleUserSchemaError(
                "Unsupported user database schema; reset the local database"
            )

        schema = _SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(schema)
        conn.execute(f"PRAGMA user_version = {USER_SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
