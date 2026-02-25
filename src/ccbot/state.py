"""Unified project-name-first state interface for ccbot.

Wraps session_manager (window_states, thread_bindings) and profile_manager
(profiles, profile_state) to present a single ProjectState keyed by project name.

Users think in project names ("arterial", "brain", "cardiac") — not window IDs
or thread IDs. This module translates between the two.

Key functions:
    get(name)              -> ProjectState | None
    list_projects()        -> list[ProjectState]
    set_project(name, ...) -> None
    find_by_thread(chat_id, thread_id) -> ProjectState | None
    find_by_window(window_id)          -> ProjectState | None
"""

import json
import subprocess
import time
import logging
from dataclasses import dataclass, asdict

from .config import config
from .session import session_manager
from .profiles import profile_manager

logger = logging.getLogger(__name__)


@dataclass
class ProjectState:
    """Unified view of a project, keyed by human-readable name.

    Merges data from:
      - session_manager.window_states  (session_id, cwd, window_name)
      - session_manager.thread_bindings (thread_id -> window_id)
      - profile_manager.profiles        (backend, model, directory)
      - profile_manager.states          (last_active, chat_id, topic_id)
      - live tmux check                 (alive)
    """

    name: str           # "arterial" — primary key (profile slug or window_name)
    window_id: str      # "@45" — internal tmux ID, hidden from users
    thread_id: int      # Telegram thread/topic ID (0 if unbound)
    chat_id: int        # Telegram chat_id (0 if unknown)
    backend: str        # "claude" | "opencode"
    model: str          # current model (empty = backend default)
    cwd: str            # working directory
    alive: bool         # tmux window exists and has a running process
    last_active: float  # Unix timestamp of last activity (0.0 if never)
    context_pct: int    # estimated context usage 0-100 (0 = unknown)
    session_id: str     # Claude/OpenCode session ID (empty if none)


def _live_window_ids() -> set[str]:
    """Return the set of currently live tmux window IDs using a synchronous
    subprocess call (safe to call outside an event loop).

    Returns empty set if tmux is not running or session does not exist.
    """
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-windows",
                "-t",
                config.tmux_session_name,
                "-F",
                "#{window_id}",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return set()
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return set()


def _window_has_process(window_id: str) -> bool:
    """Check if the pane in a window is running a non-shell process (e.g. claude)."""
    try:
        result = subprocess.run(
            [
                "tmux",
                "display-message",
                "-t",
                f"{config.tmux_session_name}:{window_id}",
                "-p",
                "#{pane_current_command}",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return False
        cmd = result.stdout.strip().lower()
        # Consider alive if running something other than shell
        return bool(cmd) and cmd not in ("bash", "zsh", "sh", "fish", "")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _build_name_to_window() -> dict[str, str]:
    """Build mapping of project_name -> window_id from session_manager state.

    Priority for name:
      1. Profile slug (if a profile's window_id matches)
      2. window_name field in WindowState
      3. window_display_names entry
      4. window_id itself (last resort)
    """
    # Build reverse: window_id -> profile_slug
    wid_to_slug: dict[str, str] = {}
    for slug, ps in profile_manager.states.items():
        if ps.window_id:
            wid_to_slug[ps.window_id] = slug

    mapping: dict[str, str] = {}
    for wid, ws in session_manager.window_states.items():
        # Name priority: profile slug > window_name > display_name > wid
        name = (
            wid_to_slug.get(wid)
            or ws.window_name
            or session_manager.window_display_names.get(wid)
            or wid
        )
        mapping[name] = wid
    return mapping


def _build_thread_lookup() -> dict[str, tuple[int, int]]:
    """Build mapping of window_id -> (chat_id, thread_id).

    Uses the first user binding found for each window. In practice there is
    typically one user, so this is unambiguous.
    """
    result: dict[str, tuple[int, int]] = {}
    for user_id, thread_id, window_id in session_manager.iter_thread_bindings():
        if window_id not in result:
            # Resolve the actual chat_id (may be a supergroup ID)
            chat_id = session_manager.resolve_chat_id(user_id, thread_id)
            result[window_id] = (chat_id, thread_id)
    return result


def _make_project_state(
    name: str,
    window_id: str,
    live_ids: set[str],
    thread_lookup: dict[str, tuple[int, int]],
) -> ProjectState:
    """Construct a ProjectState by merging all data sources for a window_id."""
    ws = session_manager.window_states.get(window_id)
    profile = profile_manager.profiles.get(name)
    pstate = profile_manager.states.get(name)

    # Core fields from window state
    session_id = ws.session_id if ws else ""
    cwd = (ws.cwd if ws else "") or (profile.directory if profile else "")

    # Backend / model from profile, fallback to config default
    backend = (profile.backend if profile else "") or config.backend
    model = (profile.model if profile else "") or ""

    # Thread / chat binding
    chat_id_thread = thread_lookup.get(window_id, (0, 0))
    chat_id = chat_id_thread[0]
    thread_id = chat_id_thread[1]

    # Prefer profile state chat_id if we don't have one from thread binding
    if chat_id == 0 and pstate and pstate.chat_id:
        chat_id = pstate.chat_id
    if thread_id == 0 and pstate and pstate.topic_id:
        thread_id = pstate.topic_id

    # last_active: prefer profile state, fallback to 0
    last_active = (pstate.last_active if pstate else 0.0) or 0.0

    # Alive check: window must exist in tmux AND have a process
    alive = window_id in live_ids and _window_has_process(window_id)

    return ProjectState(
        name=name,
        window_id=window_id,
        thread_id=thread_id,
        chat_id=chat_id,
        backend=backend,
        model=model,
        cwd=cwd,
        alive=alive,
        last_active=last_active,
        context_pct=0,  # Not currently tracked; placeholder
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get(project_name: str) -> "ProjectState | None":
    """Get full state for a project by name.

    Merges session_manager + profile_manager + live tmux check.
    Returns None if the project is unknown (no window and no profile state).
    """
    name_to_wid = _build_name_to_window()
    thread_lookup = _build_thread_lookup()
    live_ids = _live_window_ids()

    window_id = name_to_wid.get(project_name)

    # Also check if there's a profile entry (may have no active window)
    if not window_id:
        pstate = profile_manager.states.get(project_name)
        if pstate and pstate.window_id:
            window_id = pstate.window_id
        elif project_name not in profile_manager.profiles and not pstate:
            return None
        # Project exists in profiles but has no active window
        if not window_id:
            return _make_project_state_no_window(project_name)

    return _make_project_state(project_name, window_id, live_ids, thread_lookup)


def _make_project_state_no_window(name: str) -> "ProjectState":
    """Build a ProjectState for a known profile that has no active tmux window."""
    profile = profile_manager.profiles.get(name)
    pstate = profile_manager.states.get(name)

    return ProjectState(
        name=name,
        window_id="",
        thread_id=(pstate.topic_id if pstate else 0),
        chat_id=(pstate.chat_id if pstate else 0),
        backend=(profile.backend if profile else config.backend),
        model=(profile.model if profile else ""),
        cwd=(profile.directory if profile else ""),
        alive=False,
        last_active=(pstate.last_active if pstate else 0.0),
        context_pct=0,
        session_id=(pstate.last_session_id if pstate else ""),
    )


def list_projects() -> list["ProjectState"]:
    """Return all known projects with current status.

    Includes:
      - All windows in session_manager.window_states (active sessions)
      - All profiles in profile_manager (even if suspended)

    Deduplicates by project name. Window-backed entries take priority.
    """
    name_to_wid = _build_name_to_window()
    thread_lookup = _build_thread_lookup()
    live_ids = _live_window_ids()

    seen: dict[str, ProjectState] = {}

    # First pass: all windows with known state
    for name, wid in name_to_wid.items():
        ps = _make_project_state(name, wid, live_ids, thread_lookup)
        seen[name] = ps

    # Second pass: profiles that aren't in seen yet (suspended profiles)
    for slug in profile_manager.profiles:
        if slug not in seen:
            pstate = profile_manager.states.get(slug)
            if pstate and pstate.window_id and pstate.window_id not in {
                ps.window_id for ps in seen.values()
            }:
                ps = _make_project_state(slug, pstate.window_id, live_ids, thread_lookup)
                seen[slug] = ps
            elif slug not in seen:
                seen[slug] = _make_project_state_no_window(slug)

    return sorted(seen.values(), key=lambda p: p.last_active, reverse=True)


def set_project(name: str, **kwargs: object) -> None:
    """Update project state, writing back to the underlying state files.

    Supported kwargs:
        window_id (str)    — update profile_manager state
        thread_id (int)    — update profile_manager state (topic_id)
        chat_id (int)      — update profile_manager state
        session_id (str)   — update window_state and profile_manager state
        cwd (str)          — update window_state
        model (str)        — update profile definition (in-memory; profile not saved)
        last_active (float)— update profile_manager state (touch)
    """
    current = get(name)
    window_id = str(kwargs.get("window_id", current.window_id if current else ""))

    # Update session_manager window state if we have a window
    if window_id:
        ws = session_manager.get_window_state(window_id)
        if "session_id" in kwargs:
            ws.session_id = str(kwargs["session_id"])
        if "cwd" in kwargs:
            ws.cwd = str(kwargs["cwd"])
        session_manager._save_state()

    # Update profile_manager state
    pstate = profile_manager.get_state(name)
    changed = False
    if "window_id" in kwargs:
        pstate.window_id = str(kwargs["window_id"])
        changed = True
    if "thread_id" in kwargs:
        pstate.topic_id = int(kwargs["thread_id"])  # type: ignore[arg-type]
        changed = True
    if "chat_id" in kwargs:
        pstate.chat_id = int(kwargs["chat_id"])  # type: ignore[arg-type]
        changed = True
    if "session_id" in kwargs:
        pstate.last_session_id = str(kwargs["session_id"])
        changed = True
    if "last_active" in kwargs:
        pstate.last_active = float(kwargs["last_active"])  # type: ignore[arg-type]
        changed = True
    else:
        # Always touch last_active on any set_project call
        pstate.last_active = time.time()
        changed = True

    if changed:
        profile_manager._save_states()

    logger.debug("set_project(%s): %s", name, kwargs)


def find_by_thread(chat_id: int, thread_id: int) -> "ProjectState | None":
    """Reverse lookup: find project by Telegram chat_id + thread_id.

    Searches thread_bindings (via session_manager) and profile_state (topic_id).
    """
    # Search session_manager thread_bindings
    for user_id, tid, wid in session_manager.iter_thread_bindings():
        if tid != thread_id:
            continue
        resolved_chat = session_manager.resolve_chat_id(user_id, tid)
        if resolved_chat == chat_id or user_id == chat_id:
            # Find project name for this window_id
            return find_by_window(wid)

    # Fallback: search profile states by topic_id
    for slug, pstate in profile_manager.states.items():
        if pstate.topic_id == thread_id and (
            pstate.chat_id == chat_id or chat_id == 0
        ):
            return get(slug)

    return None


def find_by_window(window_id: str) -> "ProjectState | None":
    """Reverse lookup: find project by tmux window_id.

    Checks session_manager.window_states and profile_manager.states.
    """
    # Check window_states — derive name
    ws = session_manager.window_states.get(window_id)

    # Build inverse of name_to_wid
    name_to_wid = _build_name_to_window()
    wid_to_name = {v: k for k, v in name_to_wid.items()}

    name = wid_to_name.get(window_id)
    if name:
        return get(name)

    # Check profile states
    for slug, pstate in profile_manager.states.items():
        if pstate.window_id == window_id:
            return get(slug)

    # Window exists in session_manager but has no name mapping — use window_name or wid
    if ws:
        fallback_name = ws.window_name or session_manager.window_display_names.get(window_id) or window_id
        live_ids = _live_window_ids()
        thread_lookup = _build_thread_lookup()
        return _make_project_state(fallback_name, window_id, live_ids, thread_lookup)

    return None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    projects = list_projects()
    output = [asdict(p) for p in projects]
    print(json.dumps(output, indent=2, default=str))
