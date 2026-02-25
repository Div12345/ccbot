"""Session setup concierge for ccbot.

Interprets free-form user text and button callback actions, returning
structured Response objects. No Telegram imports — pure logic.

Entry points:
    handle(text, chat_id, thread_id)  — process user message
    handle_action(action)              — process inline button callback
"""

from __future__ import annotations

import difflib
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .state import (
    ProjectState,
    find_by_thread,
    get as get_project,
    list_projects,
    set_project,
)
from .discovery import scan_backends, scan_models, scan_sessions

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response types
# ---------------------------------------------------------------------------


@dataclass
class Button:
    """A single inline button."""

    label: str  # Display text, e.g. "Resume"
    data: str  # Callback data, e.g. "cc:resume:arterial"


@dataclass
class Response:
    """Structured response returned to the caller (Telegram handler etc.)."""

    type: str  # "proposal" | "status" | "action_result" | "question"
    text: str  # Message body (markdown-lite)
    buttons: list[list[Button]]  # Rows of buttons (empty list = no buttons)
    project: str | None = None  # Which project this is about


# ---------------------------------------------------------------------------
# Pending new-session setup state
# ---------------------------------------------------------------------------

# Keyed by chat_id. Each entry tracks wizard progress.
_pending_setups: dict[int, dict] = {}
# Tracks which step each chat is on: "backend" | "model" | "dir" | "confirm"
_setup_step: dict[int, str] = {}


def _clear_setup(chat_id: int) -> None:
    _pending_setups.pop(chat_id, None)
    _setup_step.pop(chat_id, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fuzzy_match_project(
    text: str, projects: list[ProjectState]
) -> ProjectState | None:
    """Return the best-matching project for text, or None."""
    names = [p.name for p in projects]
    text_lower = text.strip().lower()

    # Exact match first
    for p in projects:
        if p.name.lower() == text_lower:
            return p

    # Substring match
    for p in projects:
        if text_lower in p.name.lower() or p.name.lower() in text_lower:
            return p

    # Difflib close match
    close = difflib.get_close_matches(
        text_lower, [n.lower() for n in names], n=1, cutoff=0.6
    )
    if close:
        matched_lower = close[0]
        for p in projects:
            if p.name.lower() == matched_lower:
                return p

    return None


def _health_emoji(p: ProjectState) -> str:
    if p.alive:
        return "running"
    if p.window_id:
        return "idle"
    return "off"


def _project_one_liner(p: ProjectState) -> str:
    status = _health_emoji(p)
    model_part = f" `{p.model}`" if p.model else ""
    return f"*{p.name}* — {p.backend}{model_part} [{status}]"


def _propose_project(p: ProjectState) -> Response:
    """Build a proposal response for a known project."""
    header = _project_one_liner(p)

    if p.alive:
        text = f"{header}\nSession is running. Resume or manage it:"
        buttons = [
            [
                Button("Resume", f"cc:resume:{p.name}"),
                Button("Compact", f"cc:compact:{p.name}"),
                Button("Kill", f"cc:kill:{p.name}"),
            ]
        ]
    elif p.window_id:
        text = f"{header}\nSession window exists but is idle. Relaunch or start fresh:"
        buttons = [
            [
                Button("Relaunch", f"cc:relaunch:{p.name}"),
                Button("Fresh start", f"cc:fresh:{p.name}"),
                Button("Kill", f"cc:kill:{p.name}"),
            ]
        ]
    else:
        text = f"{header}\nNo active session. Start a new one:"
        buttons = [
            [
                Button("New session", f"cc:new:{p.name}"),
            ]
        ]

    return Response(type="proposal", text=text, buttons=buttons, project=p.name)


# ---------------------------------------------------------------------------
# New-session wizard helpers
# ---------------------------------------------------------------------------


def wizard_start_for_project(chat_id: int, project_name: str) -> Response:
    """Start the wizard with profile pre-fill (directory, name, flags).

    Skips directory selection — goes straight to backend → model → confirm.
    Called from bot layer where chat_id is available.
    """
    from .profiles import ProfileManager
    pm = ProfileManager()
    profile = pm.get(project_name)

    # Pre-fill setup state from profile
    prefill: dict = {"name": project_name}
    if profile:
        if profile.directory:
            prefill["directory"] = profile.directory
        if profile.flags:
            prefill["_flags"] = profile.flags
        if profile.system_prompt:
            prefill["_system_prompt"] = profile.system_prompt

    _pending_setups[chat_id] = prefill
    _setup_step[chat_id] = "backend"

    backends = scan_backends()
    installed = [b for b in backends if b["installed"]]
    if not installed:
        _clear_setup(chat_id)
        return Response(
            type="action_result",
            text="No backends installed. Install `claude` or `opencode` first.",
            buttons=[],
        )

    rows: list[list[Button]] = [
        [Button(b["name"], f"cc:backend:{b['id']}") for b in installed]
    ]
    dir_info = f"\nDir: `{profile.directory}`" if profile and profile.directory else ""
    return Response(
        type="question",
        text=f"Pick a backend for *{project_name}*:{dir_info}",
        buttons=rows,
    )


def _ask_backend(chat_id: int, name: str | None = None) -> Response:
    """Step 1: ask user to pick a backend."""
    backends = scan_backends()
    installed = [b for b in backends if b["installed"]]
    _pending_setups[chat_id] = {"name": name or ""}
    _setup_step[chat_id] = "backend"

    if not installed:
        _clear_setup(chat_id)
        return Response(
            type="action_result",
            text="No backends installed. Install `claude` or `opencode` first.",
            buttons=[],
        )

    rows: list[list[Button]] = [
        [Button(b["name"], f"cc:backend:{b['id']}") for b in installed]
    ]
    name_part = f" for *{name}*" if name else ""
    return Response(
        type="question",
        text=f"Pick a backend{name_part}:",
        buttons=rows,
    )


def _ask_model(chat_id: int, backend: str) -> Response:
    """Step 2: ask user to pick a model."""
    _setup_step[chat_id] = "model"
    model_data = scan_models(backend)
    rows: list[list[Button]] = []
    for provider in model_data.get("providers", []):
        row = [Button(m["name"], f"cc:model:{m['id']}") for m in provider["models"]]
        if row:
            rows.append(row)
    if not rows:
        rows = [[Button("Default", "cc:model:default")]]
    return Response(
        type="question",
        text=f"Pick a model for *{backend}*:",
        buttons=rows,
    )


def _ask_directory(chat_id: int) -> Response:
    """Step 3: ask user to pick a working directory."""
    _setup_step[chat_id] = "dir"
    # Suggest home + common project dirs
    home = Path.home()
    candidates: list[Path] = [home]
    for sub in ("projects", "code", "brain", "work", "dev"):
        p = home / sub
        if p.is_dir():
            candidates.append(p)
    # Also include any existing projects' cwds
    for proj in list_projects():
        if proj.cwd:
            cand = Path(proj.cwd)
            if cand not in candidates and cand.is_dir():
                candidates.append(cand)

    rows: list[list[Button]] = []
    for i, p in enumerate(candidates[:8]):
        rows.append([Button(str(p), f"cc:dir:{i}")])
    # Store candidates in setup state
    _pending_setups[chat_id]["_dir_candidates"] = [str(p) for p in candidates[:8]]

    return Response(
        type="question",
        text="Pick a working directory:",
        buttons=rows,
    )


def _ask_confirm(chat_id: int) -> Response:
    """Step 4: confirm and launch."""
    _setup_step[chat_id] = "confirm"
    setup = _pending_setups.get(chat_id, {})
    backend = setup.get("backend", "?")
    model = setup.get("model", "default")
    directory = setup.get("directory", "?")
    name = setup.get("name") or Path(directory).name if directory != "?" else "new"
    setup["name"] = name

    text = (
        f"*Ready to launch:*\n"
        f"Backend: `{backend}`  Model: `{model}`\n"
        f"Dir: `{directory}`  Name: `{name}`"
    )
    return Response(
        type="proposal",
        text=text,
        buttons=[
            [
                Button("Launch", "cc:go"),
                Button("Cancel", "cc:cancel"),
            ]
        ],
    )


# ---------------------------------------------------------------------------
# Tmux launch helpers
# ---------------------------------------------------------------------------


def _launch_session(
    name: str, backend: str, model: str, cwd: str, flags: str = ""
) -> tuple[bool, str]:
    """Create a tmux window and start the backend. Returns (ok, message)."""
    from .config import config  # import here to avoid circulars

    session = config.tmux_session_name

    # Create the window
    result = subprocess.run(
        ["tmux", "new-window", "-t", session, "-n", name, "-c", cwd],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Session may not exist — try creating it first
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session],
            capture_output=True,
        )
        result = subprocess.run(
            ["tmux", "new-window", "-t", session, "-n", name, "-c", cwd],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False, f"Failed to create tmux window: {result.stderr.strip()}"

    target = f"{session}:{name}"

    if backend == "claude":
        cmd = "claude"
        if model and model != "default":
            cmd += f" --model {model}"
        if flags:
            cmd += f" {flags}"
    else:
        cmd = "opencode"
        if model and model != "default":
            cmd += f" -m {model}"

    subprocess.run(
        ["tmux", "send-keys", "-t", target, cmd, "Enter"],
        capture_output=True,
    )
    return True, f"Launched *{name}* with {backend}" + (
        f" (`{model}`)" if model and model != "default" else ""
    )


def _resume_session(p: ProjectState) -> tuple[bool, str]:
    """Send Enter to wake an idle window."""
    if not p.window_id:
        return False, "No window found"
    from .config import config

    target = f"{config.tmux_session_name}:{p.window_id}"
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "", "Enter"], capture_output=True
    )
    return True, f"Sent Enter to *{p.name}*"


def _kill_session(p: ProjectState) -> tuple[bool, str]:
    """Kill a tmux window by window_id."""
    if not p.window_id:
        return False, "No window to kill"
    from .config import config

    target = f"{config.tmux_session_name}:{p.window_id}"
    result = subprocess.run(
        ["tmux", "kill-window", "-t", target],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, f"Killed window for *{p.name}*"
    return False, f"Kill failed: {result.stderr.strip()}"


def _compact_session(p: ProjectState) -> tuple[bool, str]:
    """Send /compact to the Claude session in the window."""
    if not p.window_id:
        return False, "No window found"
    from .config import config

    target = f"{config.tmux_session_name}:{p.window_id}"
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "/compact", "Enter"],
        capture_output=True,
    )
    return True, f"Sent /compact to *{p.name}*"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def handle(text: str, chat_id: int, thread_id: int | None = None) -> Response:
    """Process user intent and return a structured Response.

    Args:
        text:      Raw message text from user.
        chat_id:   Telegram chat_id.
        thread_id: Telegram thread/topic ID (None for non-topic chats).

    Returns:
        Response with type, text, buttons, and optional project name.
    """
    text = text.strip()
    text_lower = text.lower()

    # ------------------------------------------------------------------ #
    # 1. If a setup wizard is in progress and user typed something free-form
    #    during the "dir" step, treat it as a custom directory path.
    # ------------------------------------------------------------------ #
    if chat_id in _setup_step and _setup_step[chat_id] == "dir":
        p = Path(text).expanduser()
        if p.is_dir():
            _pending_setups[chat_id]["directory"] = str(p)
            return _ask_confirm(chat_id)

    # ------------------------------------------------------------------ #
    # 2. Status — show all projects
    # ------------------------------------------------------------------ #
    if text_lower in ("status", "projects", "list", "/status"):
        return _handle_status()

    # ------------------------------------------------------------------ #
    # 3. New session / launch
    # ------------------------------------------------------------------ #
    if any(kw in text_lower for kw in ("new session", "new", "launch", "start")):
        return _ask_backend(chat_id)

    # ------------------------------------------------------------------ #
    # 4. Fix intent
    # ------------------------------------------------------------------ #
    if text_lower.startswith("fix"):
        return _handle_fix(text, chat_id)

    # ------------------------------------------------------------------ #
    # 5. Project name match
    # ------------------------------------------------------------------ #
    projects = list_projects()
    matched = _fuzzy_match_project(text, projects)
    if matched:
        return _propose_project(matched)

    # ------------------------------------------------------------------ #
    # 6. Thread-bound project (silent lookup)
    # ------------------------------------------------------------------ #
    if thread_id is not None:
        bound = find_by_thread(chat_id, thread_id)
        if bound:
            return _propose_project(bound)

    # ------------------------------------------------------------------ #
    # 7. Fallback — guide the user
    # ------------------------------------------------------------------ #
    project_names = [p.name for p in projects]
    names_str = (
        "  ".join(f"`{n}`" for n in project_names[:8]) if project_names else "(none)"
    )
    return Response(
        type="question",
        text=(
            f"I can help with:\n"
            f"*Your projects:* {names_str}\n"
            f"Or say: `new session` | `status`"
        ),
        buttons=[
            [Button("Status", "cc:status"), Button("New session", "cc:new_session")]
        ],
    )


def _handle_status() -> Response:
    """Return all projects with health info."""
    projects = list_projects()
    if not projects:
        return Response(
            type="status",
            text="No projects found. Say `new session` to start one.",
            buttons=[[Button("New session", "cc:new_session")]],
        )

    lines: list[str] = []
    buttons: list[list[Button]] = []
    for p in projects:
        lines.append(_project_one_liner(p))
        row: list[Button] = []
        if p.alive:
            row.append(Button(f"Resume {p.name}", f"cc:resume:{p.name}"))
        elif p.window_id:
            row.append(Button(f"Relaunch {p.name}", f"cc:relaunch:{p.name}"))
        else:
            row.append(Button(f"New {p.name}", f"cc:new:{p.name}"))
        buttons.append(row)

    text = "\n".join(lines)
    return Response(type="status", text=text, buttons=buttons)


def _handle_fix(text: str, chat_id: int) -> Response:
    """Attempt to diagnose and fix session/MCP issues."""
    from .discovery import scan_mcps

    projects = list_projects()
    mcps = scan_mcps()

    dead_mcps = [m for m in mcps if not m["healthy"]]
    dead_sessions = [p for p in projects if p.window_id and not p.alive]

    parts: list[str] = []
    if dead_mcps:
        names = ", ".join(f"`{m['name']}`" for m in dead_mcps)
        parts.append(f"Unhealthy MCPs: {names}")
    if dead_sessions:
        names = ", ".join(f"`{p.name}`" for p in dead_sessions)
        parts.append(f"Dead sessions: {names}")

    if not parts:
        return Response(
            type="action_result",
            text="Everything looks healthy. No fixes needed.",
            buttons=[],
        )

    text_out = "Found issues:\n" + "\n".join(parts)
    buttons: list[list[Button]] = []
    for p in dead_sessions:
        buttons.append([Button(f"Relaunch {p.name}", f"cc:relaunch:{p.name}")])

    return Response(type="action_result", text=text_out, buttons=buttons)


def handle_action(action: str) -> Response:
    """Handle button callback actions like 'cc:resume:arterial'.

    Args:
        action: Callback data string from inline button press.

    Returns:
        Response describing the result.
    """
    parts = action.split(":", 2)
    if not parts or parts[0] != "cc":
        return Response(type="action_result", text="Unknown action.", buttons=[])

    if len(parts) < 2:
        return Response(type="action_result", text="Malformed action.", buttons=[])

    verb = parts[1]
    arg = parts[2] if len(parts) > 2 else ""

    # ------------------------------------------------------------------ #
    # Status shortcut
    # ------------------------------------------------------------------ #
    if verb == "status":
        return _handle_status()

    # ------------------------------------------------------------------ #
    # New session shortcut (no project name)
    # ------------------------------------------------------------------ #
    if verb == "new_session":
        # We don't have chat_id here — caller must handle wizard init
        # Return a question prompting the caller to restart with handle()
        return Response(
            type="question",
            text="Send `new session` to start the setup wizard.",
            buttons=[],
        )

    # ------------------------------------------------------------------ #
    # New session for a named project
    # ------------------------------------------------------------------ #
    if verb == "new":
        # Action callbacks don't include chat_id, so launch wizard from bot command.
        return Response(
            type="question",
            text=f"Use /new in your topic to launch a new session for *{arg}*.",
            buttons=[],
            project=arg,
        )

    # ------------------------------------------------------------------ #
    # Cancel wizard
    # ------------------------------------------------------------------ #
    if verb == "cancel":
        return Response(type="action_result", text="Setup cancelled.", buttons=[])

    # ------------------------------------------------------------------ #
    # Backend selection (wizard step 1 → step 2)
    # ------------------------------------------------------------------ #
    if verb == "backend":
        # We don't have chat_id in handle_action; caller must pass it
        # through a wrapper or store it separately. Return a marker response
        # so the bot layer can call _handle_backend_selected(chat_id, arg).
        return Response(
            type="question",
            text=f"Backend *{arg}* selected. Now pick a model.",
            buttons=[],
            project=arg,  # reuse project field to carry backend id
        )

    # ------------------------------------------------------------------ #
    # Model selection (wizard step 2 → step 3)
    # ------------------------------------------------------------------ #
    if verb == "model":
        return Response(
            type="question",
            text=f"Model *{arg}* selected. Now pick a directory.",
            buttons=[],
            project=arg,
        )

    # ------------------------------------------------------------------ #
    # Directory selection (wizard step 3 → step 4)
    # ------------------------------------------------------------------ #
    if verb == "dir":
        return Response(
            type="question",
            text=f"Directory index *{arg}* selected. Confirm to launch.",
            buttons=[],
            project=arg,
        )

    # ------------------------------------------------------------------ #
    # Go — execute pending setup
    # ------------------------------------------------------------------ #
    if verb == "go":
        # chat_id must be provided by caller; here we return a signal
        return Response(
            type="action_result",
            text="Launching… (call handle_action_with_context to execute)",
            buttons=[],
        )

    # ------------------------------------------------------------------ #
    # Project actions
    # ------------------------------------------------------------------ #
    project_name = arg
    p = get_project(project_name)

    if verb == "resume":
        if not p:
            return Response(
                type="action_result",
                text=f"Project `{project_name}` not found.",
                buttons=[],
            )
        ok, msg = _resume_session(p)
        return Response(
            type="action_result",
            text=msg,
            buttons=[[Button("Status", "cc:status")]],
            project=project_name,
        )

    if verb == "compact":
        if not p:
            return Response(
                type="action_result",
                text=f"Project `{project_name}` not found.",
                buttons=[],
            )
        ok, msg = _compact_session(p)
        return Response(
            type="action_result",
            text=msg,
            buttons=[[Button("Status", "cc:status")]],
            project=project_name,
        )

    if verb == "kill":
        if not p:
            return Response(
                type="action_result",
                text=f"Project `{project_name}` not found.",
                buttons=[],
            )
        ok, msg = _kill_session(p)
        return Response(
            type="action_result",
            text=msg,
            buttons=[[Button("Status", "cc:status")]],
            project=project_name,
        )

    if verb in ("fresh", "relaunch"):
        if not p:
            return Response(
                type="action_result",
                text=f"Project `{project_name}` not found.",
                buttons=[],
            )
        # Kill existing window first
        if p.window_id:
            _kill_session(p)
            time.sleep(0.5)

        backend = p.backend or "claude"
        model = p.model or "default"
        cwd = p.cwd or str(Path.home())
        ok, msg = _launch_session(project_name, backend, model, cwd)
        return Response(
            type="action_result",
            text=msg,
            buttons=[[Button("Status", "cc:status")]],
            project=project_name,
        )

    return Response(
        type="action_result",
        text=f"Unknown action verb: `{verb}`",
        buttons=[],
    )


# ---------------------------------------------------------------------------
# Stateful wizard helpers for bot layer
# ---------------------------------------------------------------------------


def wizard_select_backend(chat_id: int, backend: str) -> Response:
    """Called by bot layer when user picks a backend button."""
    if chat_id not in _pending_setups:
        _pending_setups[chat_id] = {}
    _pending_setups[chat_id]["backend"] = backend
    return _ask_model(chat_id, backend)


def wizard_select_model(chat_id: int, model: str) -> Response:
    """Called by bot layer when user picks a model button."""
    _pending_setups.setdefault(chat_id, {})["model"] = model
    # Skip directory step if already pre-filled from profile
    if _pending_setups[chat_id].get("directory"):
        return _ask_confirm(chat_id)
    return _ask_directory(chat_id)


def wizard_select_dir(chat_id: int, index: int) -> Response:
    """Called by bot layer when user picks a directory button."""
    setup = _pending_setups.get(chat_id, {})
    candidates = setup.get("_dir_candidates", [])
    if 0 <= index < len(candidates):
        setup["directory"] = candidates[index]
        return _ask_confirm(chat_id)
    return Response(
        type="action_result",
        text=f"Invalid directory index {index}.",
        buttons=[],
    )


def wizard_go(chat_id: int) -> Response:
    """Called by bot layer when user confirms the wizard."""
    setup = _pending_setups.get(chat_id, {})
    backend = setup.get("backend", "claude")
    model = setup.get("model", "default")
    directory = setup.get("directory", str(Path.home()))
    name = setup.get("name") or Path(directory).name or "session"
    flags = setup.get("_flags", "")
    system_prompt = setup.get("_system_prompt", "")

    _clear_setup(chat_id)
    ok, msg = _launch_session(name, backend, model, directory, flags=flags)

    # Inject system prompt after launch
    if ok and system_prompt:
        import time
        from .config import config as _cfg
        time.sleep(1)
        target = f"{_cfg.tmux_session_name}:{name}"
        subprocess.run(
            ["tmux", "send-keys", "-t", target, system_prompt, "Enter"],
            capture_output=True,
        )
        msg += "\n_System prompt injected._"

    return Response(
        type="action_result",
        text=msg,
        buttons=[[Button("Status", "cc:status")]],
        project=name,
    )


# ---------------------------------------------------------------------------
# __main__ smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    def _show(label: str, r: Response) -> None:
        print(f"\n=== {label} ===")
        print(f"type:    {r.type}")
        print(f"project: {r.project}")
        print(f"text:\n{r.text}")
        if r.buttons:
            for row in r.buttons:
                labels = " | ".join(f"[{b.label} → {b.data}]" for b in row)
                print(f"buttons: {labels}")

    print("Simulating handle() calls (no live tmux/state required)\n")

    r1 = handle("arterial", 123)
    _show("handle('arterial', 123)", r1)

    r2 = handle("status", 123)
    _show("handle('status', 123)", r2)

    r3 = handle("new session", 123)
    _show("handle('new session', 123)", r3)
