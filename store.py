"""Pending suggestions, shared by the two halves of the plugin.

The hook that writes a suggestion (``__init__.py``) and the route that serves
it to Desktop (``dashboard/plugin_api.py``) can run in different processes:
a gateway serving several profiles runs a secondary profile's dashboard
plugin API in that profile's plugin host, while that profile's turns (and so
its hooks) run in the gateway. Memory is not shared between them, so this
SQLite file in the plugin's data directory is the only state both halves
read and write. WAL lets one process read while the other writes.

One row per session in ``pending``: the pending suggestion (``text``, plus up
to two ``alternatives`` as a JSON list when the agent offered a choice), or
the reason the last one could not be generated. ``turns`` keeps each session's last trimmed
context, so "generate suggestion" also works after a restart. ``requests`` carries a
"generate suggestion" from a process without the hooks (the dashboard API in a plugin host, on a
backend with no plugin RPC) to one that runs them; ``worker.alive`` in the data directory says one
is running, ``regenerate.request`` that a request is waiting. Both halves load this file by path (the host imports
``plugin_api.py`` with no package), so it holds no state of its own.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

_regenerator: Optional[Callable[[str], bool]] = None


def set_regenerator(fn: Optional[Callable[[str], bool]]) -> None:
    """Register the in-process hook callback for manual regeneration."""
    global _regenerator
    _regenerator = fn


def regenerate(session_id: str) -> Optional[bool]:
    """Trigger regeneration if the hook half lives in this process, else None."""
    if _regenerator is None or not session_id:
        return None
    return bool(_regenerator(session_id))

PLUGIN_NAME = "next-prompt"
DB_FILE = "suggestions.db"
SUGGESTION_TTL_SECONDS = 7 * 24 * 3600
# A failure note only explains the latest turn; after a restart (often the
# fix) an old one would just be noise.
FAILURE_TTL_SECONDS = 6 * 3600
MAX_ROWS = 200
# Last turn per session for manual regeneration: the trimmed context the
# prompt uses (not the transcript), the most recent sessions only.
TURN_TTL_SECONDS = SUGGESTION_TTL_SECONDS
MAX_TURNS = 50
# A queued "generate suggestion" is only worth serving while the user still
# waits for it; a process that runs the hooks marks itself alive this often.
REQUEST_TTL_SECONDS = 30.0
WORKER_FRESH_SECONDS = 15.0
REQUEST_FILE = "regenerate.request"
WORKER_FILE = "worker.alive"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending (
    session_id TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    text       TEXT,
    failure    TEXT,
    timestamp  REAL NOT NULL
)
"""

_TURNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    session_id TEXT PRIMARY KEY,
    context    TEXT NOT NULL,
    last_user  TEXT,
    timestamp  REAL NOT NULL
)
"""

_REQUESTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    session_id   TEXT PRIMARY KEY,
    requested_at REAL NOT NULL
)
"""


def data_dir() -> Optional[Path]:
    """``<hermes home>/plugin-data/next-prompt/`` of the profile in scope now.

    Resolved on every call: the request or turn decides the profile.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir
    except ImportError:  # hosts before the plugin storage helpers
        plugin_data_dir = None
    try:
        if plugin_data_dir is not None:
            return plugin_data_dir(PLUGIN_NAME)
        from hermes_constants import get_hermes_home

        path = get_hermes_home() / "plugin-data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path
    except Exception as exc:
        logger.warning("next-prompt: no data directory: %s", exc)
        return None


def _connect() -> Optional[sqlite3.Connection]:
    try:
        from plugins.plugin_storage import plugin_db
    except ImportError:
        plugin_db = None
    try:
        if plugin_db is not None:
            conn = plugin_db(PLUGIN_NAME, DB_FILE)
        else:
            directory = data_dir()
            if directory is None:
                return None
            conn = sqlite3.connect(str(directory / DB_FILE), timeout=5.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SCHEMA)
        conn.execute(_TURNS_SCHEMA)
        conn.execute(_REQUESTS_SCHEMA)
        _add_alternatives_column(conn)
        return conn
    except Exception as exc:
        logger.warning("next-prompt: could not open %s: %s", DB_FILE, exc)
        return None


def _add_alternatives_column(conn: sqlite3.Connection) -> None:
    """Stores from 0.3.5 and earlier have no ``alternatives`` column."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(pending)")}
    if "alternatives" in columns:
        return
    try:
        conn.execute("ALTER TABLE pending ADD COLUMN alternatives TEXT")
    except sqlite3.OperationalError:
        pass  # the other process added it first


def _alternatives(raw: Optional[str]) -> List[str]:
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []


def _write(
    session_id: str, kind: str, text: Optional[str], failure: Optional[str], alternatives: Sequence[str] = ()
) -> Optional[float]:
    conn = _connect()
    if conn is None:
        return None
    now = time.time()
    extra = json.dumps(list(alternatives), ensure_ascii=False) if alternatives else None
    try:
        with closing(conn), conn:
            conn.execute(
                "INSERT OR REPLACE INTO pending (session_id, kind, text, failure, timestamp, alternatives) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, kind, text, failure, now, extra),
            )
            conn.execute(
                "DELETE FROM pending WHERE (kind = 'suggestion' AND timestamp < ?) OR (kind != 'suggestion' AND timestamp < ?)",
                (now - SUGGESTION_TTL_SECONDS, now - FAILURE_TTL_SECONDS),
            )
            conn.execute(
                "DELETE FROM pending WHERE session_id NOT IN "
                "(SELECT session_id FROM pending ORDER BY timestamp DESC LIMIT ?)",
                (MAX_ROWS,),
            )
    except sqlite3.Error as exc:
        logger.warning("next-prompt: could not save the %s for %s: %s", kind, session_id, exc)
        return None
    return now


def put_suggestion(session_id: str, text: str, alternatives: Sequence[str] = ()) -> Optional[Dict[str, Any]]:
    """Store the session's pending suggestion, replacing whatever it had.

    ``text`` is the most likely reply; ``alternatives`` the other options the
    agent offered, in order."""
    alternatives = [item for item in alternatives if item and item != text]
    timestamp = _write(session_id, "suggestion", text, None, alternatives)
    if timestamp is None:
        return None
    return _suggestion(session_id, text, alternatives, timestamp)


def _suggestion(session_id: str, text: str, alternatives: Sequence[str], timestamp: float) -> Dict[str, Any]:
    # ``text`` stays the most likely reply for clients that show one pill;
    # ``options`` is every reply to show, ``text`` first.
    return {"text": text, "options": [text, *alternatives], "session_id": session_id, "timestamp": timestamp}


def put_failure(session_id: str, kind: str) -> None:
    """Record why this turn has no suggestion (a coarse kind, never error text)."""
    _write(session_id, "failure", None, kind)


def clear(session_id: str) -> None:
    """Drop the session's suggestion or failure note (used, dismissed, superseded)."""
    conn = _connect()
    if conn is None:
        return
    try:
        with closing(conn), conn:
            conn.execute("DELETE FROM pending WHERE session_id = ?", (session_id,))
    except sqlite3.Error as exc:
        logger.warning("next-prompt: could not clear %s: %s", session_id, exc)


def get(session_id: str) -> Dict[str, Optional[Dict[str, Any]]]:
    """``{"suggestion": …, "error": …}`` for one session, the REST payload."""
    empty: Dict[str, Optional[Dict[str, Any]]] = {"suggestion": None, "error": None}
    if not session_id:
        return empty
    conn = _connect()
    if conn is None:
        return empty
    try:
        with closing(conn):
            row = conn.execute(
                "SELECT kind, text, failure, timestamp, alternatives FROM pending WHERE session_id = ?", (session_id,)
            ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("next-prompt: could not read %s: %s", session_id, exc)
        return empty
    if not row:
        return empty
    kind, text, failure, timestamp, alternatives = row
    age = time.time() - float(timestamp or 0)
    if kind == "suggestion" and text and age < SUGGESTION_TTL_SECONDS:
        return {"suggestion": _suggestion(session_id, text, _alternatives(alternatives), timestamp), "error": None}
    if kind == "failure" and failure and age < FAILURE_TTL_SECONDS:
        return {"suggestion": None, "error": {"kind": failure, "session_id": session_id, "timestamp": timestamp}}
    return empty


def count() -> int:
    """Pending suggestions in the store (for the registration log line)."""
    conn = _connect()
    if conn is None:
        return 0
    try:
        with closing(conn):
            row = conn.execute(
                "SELECT COUNT(*) FROM pending WHERE kind = 'suggestion' AND timestamp >= ?",
                (time.time() - SUGGESTION_TTL_SECONDS,),
            ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def import_legacy(entries: Dict[str, Any]) -> int:
    """Carry pending suggestions over from the 0.3.0 ``suggestions.json``.

    Only sessions with no row yet; keeps each entry's original timestamp.
    """
    conn = _connect()
    if conn is None:
        return 0
    now = time.time()
    rows = [
        (sid, entry["text"], float(entry.get("timestamp") or 0))
        for sid, entry in entries.items()
        if isinstance(entry, dict) and entry.get("text")
        and now - float(entry.get("timestamp") or 0) < SUGGESTION_TTL_SECONDS
    ]
    try:
        with closing(conn), conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO pending (session_id, kind, text, failure, timestamp) "
                "VALUES (?, 'suggestion', ?, NULL, ?)",
                rows,
            )
            return conn.total_changes - before
    except sqlite3.Error as exc:
        logger.warning("next-prompt: could not import saved suggestions: %s", exc)
        return 0


def put_turn(session_id: str, context: str, last_user: str) -> bool:
    """Remember the session's last trimmed context for "generate suggestion"."""
    if not session_id or not (context or "").strip():
        return False
    conn = _connect()
    if conn is None:
        return False
    now = time.time()
    try:
        with closing(conn), conn:
            conn.execute(
                "INSERT OR REPLACE INTO turns (session_id, context, last_user, timestamp) VALUES (?, ?, ?, ?)",
                (session_id, context, last_user or "", now),
            )
            conn.execute("DELETE FROM turns WHERE timestamp < ?", (now - TURN_TTL_SECONDS,))
            conn.execute(
                "DELETE FROM turns WHERE session_id NOT IN "
                "(SELECT session_id FROM turns ORDER BY timestamp DESC LIMIT ?)",
                (MAX_TURNS,),
            )
    except sqlite3.Error as exc:
        logger.warning("next-prompt: could not save the last turn of %s: %s", session_id, exc)
        return False
    return True


def get_turn(session_id: str) -> Optional[tuple]:
    """``(context, last_user)`` of the session's last turn, or None."""
    if not session_id:
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with closing(conn):
            row = conn.execute(
                "SELECT context, last_user, timestamp FROM turns WHERE session_id = ?", (session_id,)
            ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("next-prompt: could not read the last turn of %s: %s", session_id, exc)
        return None
    if not row or not row[0] or time.time() - float(row[2] or 0) >= TURN_TTL_SECONDS:
        return None
    return row[0], row[1] or ""


# ── "generate suggestion" across processes ──────────────────────────────


def _marker_age(name: str) -> Optional[float]:
    directory = data_dir()
    if directory is None:
        return None
    try:
        return time.time() - (directory / name).stat().st_mtime
    except OSError:
        return None


def _touch_marker(name: str) -> None:
    directory = data_dir()
    if directory is None:
        return
    try:
        path = directory / name
        path.touch(exist_ok=True)
        os.utime(path, None)
    except OSError as exc:
        logger.debug("next-prompt: could not touch %s: %s", name, exc)


def mark_worker_alive() -> None:
    """A process that runs the hooks can serve queued requests right now."""
    _touch_marker(WORKER_FILE)


def worker_alive() -> bool:
    age = _marker_age(WORKER_FILE)
    return age is not None and age < WORKER_FRESH_SECONDS


def request_stamp() -> Optional[float]:
    """When the last request was queued (the marker's mtime), else None.

    A cheap check for the polling side: it opens the database only when
    this changes."""
    directory = data_dir()
    if directory is None:
        return None
    try:
        return (directory / REQUEST_FILE).stat().st_mtime
    except OSError:
        return None


def request_regeneration(session_id: str) -> Optional[bool]:
    """Queue "generate suggestion" for a process that runs the hooks.

    None when no such process is alive (or the store is unreachable), False
    when the session has no saved turn to generate from, True when queued."""
    if not session_id or not worker_alive():
        return None
    if get_turn(session_id) is None:
        return False
    conn = _connect()
    if conn is None:
        return None
    try:
        with closing(conn), conn:
            conn.execute(
                "INSERT OR REPLACE INTO requests (session_id, requested_at) VALUES (?, ?)",
                (session_id, time.time()),
            )
    except sqlite3.Error as exc:
        logger.warning("next-prompt: could not queue a suggestion for %s: %s", session_id, exc)
        return None
    _touch_marker(REQUEST_FILE)
    return True


def claim_requests() -> List[str]:
    """Take every queued request still worth serving, oldest first.

    The delete and the read are one transaction, so when several processes
    run the hooks only one of them gets each request."""
    conn = _connect()
    if conn is None:
        return []
    try:
        with closing(conn):
            conn.execute("BEGIN IMMEDIATE")  # the write lock first: no other process reads these rows
            try:
                rows = conn.execute(
                    "SELECT session_id, requested_at FROM requests ORDER BY requested_at"
                ).fetchall()
                conn.execute("DELETE FROM requests")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    except sqlite3.Error as exc:
        logger.warning("next-prompt: could not read queued suggestions: %s", exc)
        return []
    cutoff = time.time() - REQUEST_TTL_SECONDS
    return [row[0] for row in rows if float(row[1] or 0) >= cutoff]
