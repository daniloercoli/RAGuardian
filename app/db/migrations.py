"""Database schema initialization.

Running ``init_schema(db_path)`` creates the ``users`` and ``api_keys``
tables (if they do not already exist) from ``schema.sql``.

The function is safe to call on every startup: the SQL uses
``CREATE TABLE IF NOT EXISTS``, so existing data is never lost.
"""

from __future__ import annotations

from pathlib import Path

from db.connection import get_connection

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def init_schema(db_path: str | Path) -> None:
    """Create tables and indexes from schema.sql, then close the connection."""
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn = get_connection(db_path)
    try:
        conn.executescript(schema)
    finally:
        conn.close()
