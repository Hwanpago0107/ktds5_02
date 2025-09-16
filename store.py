"""Store factory.

This module keeps store selection logic minimal and stable. It returns a
SQLite-backed store when available, or ``None`` to safely fall back to the
in-memory code paths used elsewhere in the app.
"""

from typing import Optional

from sqlite_store import SQLiteStore


def build_store_from_env() -> Optional[SQLiteStore]:
    """Build a store instance from environment.

    Behavior is intentionally simple: always attempt SQLite initialization and
    return ``None`` if anything fails so that the rest of the application can
    operate in in-memory mode without raising.
    """
    try:
        store = SQLiteStore()
        return store if store.enabled else None
    except Exception:
        return None

