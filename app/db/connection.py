"""SQLite connection factory shared by UserStore and migration code.

Centralizing connection setup here keeps SQLite pragmas consistent across
the whole application. Every connection uses the same configuration:

* ``WAL`` journal mode  - allows concurrent readers while a write is in
  progress, which is important for a web server.
* ``busy_timeout``      - how long SQLite waits (in ms) for a lock before
  raising ``SQLITE_BUSY``. 30 seconds is a safe default.
* ``foreign_keys = ON`` - enforces ``ON DELETE CASCADE`` rules declared in
  ``schema.sql`` (e.g. deleting a user also deletes their API keys).
* ``row_factory = Row`` - lets us access columns by name (``row["email"]``)
  instead of by index, making the code more readable.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open a new SQLite connection with the standard pragmas applied."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
