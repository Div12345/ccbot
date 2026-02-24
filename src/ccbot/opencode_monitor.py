"""OpenCode session monitor — polls SQLite for new messages.

Replacement for SessionMonitor when CCBOT_BACKEND=opencode.
Matches tmux windows to OpenCode sessions by directory path,
then polls for new parts and emits NewMessage callbacks.

Session discovery: instead of Claude Code's hook-based session_map.json,
this monitor queries OpenCode's SQLite database and matches sessions to
tmux windows by their working directory.

Key class: OpenCodeMonitor (same interface as SessionMonitor).
"""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import config
from .session_monitor import NewMessage
from .tmux_manager import tmux_manager
from . import opencode_parser

logger = logging.getLogger(__name__)


class OpenCodeMonitor:
    """Monitors OpenCode sessions via SQLite polling.

    Same public interface as SessionMonitor:
      - set_message_callback()
      - start()
      - stop()

    Internally, polls OpenCode's SQLite database instead of JSONL files.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        poll_interval: float | None = None,
    ):
        self.db_path = db_path or config.opencode_db_path
        self.poll_interval = (
            poll_interval if poll_interval is not None else config.monitor_poll_interval
        )
        self._running = False
        self._task: asyncio.Task | None = None
        self._message_callback: Callable[[NewMessage], Awaitable[None]] | None = None
        # Track last seen part time_created per session (unix ms)
        self._last_part_time: dict[str, int] = {}
        # Cache message roles: message_id -> role
        self._message_roles: dict[str, str] = {}

    def set_message_callback(
        self, callback: Callable[[NewMessage], Awaitable[None]]
    ) -> None:
        self._message_callback = callback

    def _get_db(self) -> sqlite3.Connection:
        """Open a read-only SQLite connection."""
        db = sqlite3.connect(
            f"file:{self.db_path}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        db.row_factory = sqlite3.Row
        return db

    def _discover_sessions_sync(
        self, cwd_to_window: dict[str, str]
    ) -> dict[str, tuple[str, str]]:
        """Match tmux window cwds to OpenCode sessions (runs in thread).

        Args:
            cwd_to_window: {resolved_cwd: window_id}

        Returns:
            {window_id: (session_id, directory)}
        """
        result: dict[str, tuple[str, str]] = {}
        try:
            db = self._get_db()
            for cwd, window_id in cwd_to_window.items():
                row = db.execute(
                    "SELECT id, directory FROM session "
                    "WHERE directory = ? "
                    "ORDER BY time_updated DESC LIMIT 1",
                    (cwd,),
                ).fetchone()
                if row:
                    result[window_id] = (row["id"], row["directory"])
            db.close()
        except sqlite3.Error as e:
            logger.error("SQLite error discovering sessions: %s", e)
        return result

    async def _discover_sessions(self) -> dict[str, tuple[str, str]]:
        """Match tmux windows to OpenCode sessions by directory.

        Returns: {window_id: (session_id, directory)}
        """
        windows = await tmux_manager.list_windows()
        if not windows:
            return {}

        cwd_to_window: dict[str, str] = {}
        for w in windows:
            try:
                resolved = str(Path(w.cwd).resolve())
            except (OSError, ValueError):
                resolved = w.cwd
            cwd_to_window[resolved] = w.window_id

        if not cwd_to_window:
            return {}

        return await asyncio.to_thread(
            self._discover_sessions_sync, cwd_to_window
        )

    def _query_new_parts_sync(
        self, session_id: str, since_time: int
    ) -> list[dict[str, Any]]:
        """Query new parts from SQLite (runs in thread).

        Returns list of part data dicts with injected metadata:
          _message_id, _timestamp, _time_created (raw unix ms)
        """
        parts: list[dict[str, Any]] = []
        try:
            db = self._get_db()
            rows = db.execute(
                "SELECT p.id, p.message_id, p.time_created, p.data, "
                "       m.data AS message_data "
                "FROM part p "
                "JOIN message m ON m.id = p.message_id "
                "WHERE p.session_id = ? AND p.time_created > ? "
                "ORDER BY p.time_created ASC",
                (session_id, since_time),
            ).fetchall()

            for row in rows:
                try:
                    part_data = json.loads(row["data"])
                    msg_data = json.loads(row["message_data"])

                    # Inject metadata for parser and cursor tracking
                    part_data["_message_id"] = row["message_id"]
                    part_data["_time_created"] = row["time_created"]
                    part_data["_timestamp"] = datetime.fromtimestamp(
                        row["time_created"] / 1000, tz=timezone.utc
                    ).isoformat()

                    # Cache message role
                    role = msg_data.get("role", "assistant")
                    self._message_roles[row["message_id"]] = role

                    parts.append(part_data)
                except (json.JSONDecodeError, KeyError) as e:
                    logger.debug("Error parsing part %s: %s", row["id"], e)

            db.close()
        except sqlite3.Error as e:
            logger.error(
                "SQLite error querying parts for %s: %s", session_id[:16], e
            )
        return parts

    def _init_cursors_sync(
        self, sessions: dict[str, tuple[str, str]]
    ) -> dict[str, int]:
        """Initialize part cursors to current max time (runs in thread)."""
        cursors: dict[str, int] = {}
        try:
            db = self._get_db()
            for _window_id, (session_id, _directory) in sessions.items():
                row = db.execute(
                    "SELECT MAX(time_created) AS max_time FROM part "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                cursors[session_id] = (
                    row["max_time"] if row and row["max_time"] else 0
                )
            db.close()
        except sqlite3.Error as e:
            logger.error("SQLite error initializing cursors: %s", e)
        return cursors

    async def _init_cursors(self) -> None:
        """Set part cursors to current state so we don't replay old messages."""
        sessions = await self._discover_sessions()
        cursors = await asyncio.to_thread(self._init_cursors_sync, sessions)
        self._last_part_time = cursors

        for session_id, cursor in cursors.items():
            logger.info(
                "Initialized cursor for session %s: %d",
                session_id[:16],
                cursor,
            )

    async def _poll(self) -> None:
        """Single poll cycle: discover sessions, check for new parts, emit."""
        # Deferred import to avoid circular dependency
        from .session import session_manager

        window_sessions = await self._discover_sessions()

        # Update session_manager window_states so find_users works
        for window_id, (session_id, directory) in window_sessions.items():
            state = session_manager.get_window_state(window_id)
            if state.session_id != session_id:
                old_sid = state.session_id
                state.session_id = session_id
                state.cwd = directory
                if old_sid:
                    logger.info(
                        "Window %s session changed: %s -> %s",
                        window_id,
                        old_sid[:16],
                        session_id[:16],
                    )
                else:
                    logger.info(
                        "Mapped window %s -> session %s (%s)",
                        window_id,
                        session_id[:16],
                        directory,
                    )

        # Collect unique active session IDs
        active_sessions: dict[str, str] = {}  # session_id -> directory
        for _wid, (sid, directory) in window_sessions.items():
            active_sessions[sid] = directory

        # Poll each session for new parts
        for session_id in active_sessions:
            since = self._last_part_time.get(session_id, 0)

            new_parts = await asyncio.to_thread(
                self._query_new_parts_sync, session_id, since
            )
            if not new_parts:
                continue

            logger.debug(
                "Read %d new parts for session %s",
                len(new_parts),
                session_id[:16],
            )

            # Parse into display entries
            parsed = opencode_parser.parse_parts(new_parts, self._message_roles)

            # Emit messages via callback
            for entry in parsed:
                if not entry.text:
                    continue
                # Skip user messages unless configured to show
                if entry.role == "user" and not config.show_user_messages:
                    continue
                if self._message_callback:
                    try:
                        await self._message_callback(
                            NewMessage(
                                session_id=session_id,
                                text=entry.text,
                                is_complete=True,
                                content_type=entry.content_type,
                                tool_use_id=entry.tool_use_id,
                                role=entry.role,
                                tool_name=entry.tool_name,
                            )
                        )
                    except Exception as e:
                        logger.error("Message callback error: %s", e)

            # Advance cursor to latest part time
            max_time = max(p["_time_created"] for p in new_parts)
            self._last_part_time[session_id] = max(
                self._last_part_time.get(session_id, 0), max_time
            )

    async def _monitor_loop(self) -> None:
        """Background polling loop."""
        logger.info(
            "OpenCode monitor started, polling every %ss (db: %s)",
            self.poll_interval,
            self.db_path,
        )

        await self._init_cursors()

        while self._running:
            try:
                await self._poll()
            except Exception as e:
                logger.error("Monitor loop error: %s", e)

            await asyncio.sleep(self.poll_interval)

        logger.info("OpenCode monitor stopped")

    def start(self) -> None:
        """Start the background polling loop."""
        if self._running:
            logger.warning("Monitor already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("OpenCode monitor stopped and cleanup done")
