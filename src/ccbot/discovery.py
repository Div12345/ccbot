"""System scanning module for ccbot.

Discovers installed backends, available models, MCP server health, and
active tmux sessions — with no Telegram or bot dependencies.

Functions:
    scan_backends()  → list of backend info dicts
    scan_models()    → provider/model hierarchy for a backend
    scan_mcps()      → MCP server health from ~/.mcp.json
    scan_sessions()  → tmux window list with liveness info
    scan_all()       → combined dict of all the above

Run directly to print a formatted JSON snapshot:
    python -m ccbot.discovery
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

_BACKEND_META: dict[str, dict[str, str]] = {
    "claude": {"cli": "claude", "name": "Claude Code"},
    "opencode": {"cli": "opencode", "name": "OpenCode"},
}


def scan_backends() -> list[dict[str, Any]]:
    """Discover installed CLI backends by checking PATH.

    Returns:
        List of dicts: {name, cli, installed, version}
    """
    results: list[dict[str, Any]] = []
    for backend_id, meta in _BACKEND_META.items():
        cli = meta["cli"]
        installed = shutil.which(cli) is not None
        version: str | None = None
        if installed:
            version = _get_version(cli)
        results.append({
            "id": backend_id,
            "name": meta["name"],
            "cli": cli,
            "installed": installed,
            "version": version,
        })
    return results


def _get_version(cli: str) -> str | None:
    """Return version string for a CLI tool, or None on failure."""
    for flag in ("--version", "version"):
        try:
            proc = subprocess.run(
                [cli, flag],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = (proc.stdout + proc.stderr).strip()
            if output:
                # Take just the first line to avoid wall-of-text
                return output.splitlines()[0]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
    return None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

def scan_models(backend: str) -> dict[str, list[dict[str, Any]]]:
    """Discover available models for a backend.

    Args:
        backend: "claude" or "opencode"

    Returns:
        {"providers": [{"id", "name", "models": [{"id", "name"}]}]}
    """
    result: dict[str, list] = {"providers": []}

    if backend == "claude":
        result["providers"].append({
            "id": "claude",
            "name": "Claude Code",
            "models": [
                {"id": "default", "name": "Default"},
                {"id": "opus", "name": "Opus (strongest)"},
                {"id": "sonnet", "name": "Sonnet (balanced)"},
                {"id": "haiku", "name": "Haiku (fast)"},
            ],
        })

    elif backend == "opencode":
        # 1. Custom models from opencode.json (user-configured, shown first)
        custom_models: list[dict[str, str]] = []
        oc_config = Path.home() / ".config" / "opencode" / "opencode.json"
        if oc_config.exists():
            try:
                data = json.loads(oc_config.read_text(encoding="utf-8"))
                for _prov, prov_data in data.get("provider", {}).items():
                    for mid, minfo in prov_data.get("models", {}).items():
                        display = minfo.get("name", mid)
                        if len(mid) <= 55:
                            custom_models.append({"id": mid, "name": display})
            except (ValueError, KeyError, OSError):
                pass

        if custom_models:
            result["providers"].append({
                "id": "_custom",
                "name": "Configured",
                "models": [{"id": "default", "name": "Default"}] + custom_models,
            })

        # 2. Live models from `opencode models`
        try:
            proc = subprocess.run(
                ["opencode", "models"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0:
                grouped: dict[str, list[dict[str, str]]] = {}
                for line in proc.stdout.strip().splitlines():
                    line = line.strip()
                    if "/" not in line or len(line) > 55:
                        continue
                    provider, model_short = line.split("/", 1)
                    grouped.setdefault(provider, []).append(
                        {"id": line, "name": model_short}
                    )
                for prov_id, models in sorted(grouped.items()):
                    result["providers"].append({
                        "id": prov_id,
                        "name": prov_id.title(),
                        "models": models,
                    })
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

        # Ensure at least a default entry
        if not result["providers"]:
            result["providers"].append({
                "id": "_default",
                "name": "OpenCode",
                "models": [{"id": "default", "name": "Default"}],
            })

    return result


# ---------------------------------------------------------------------------
# MCPs
# ---------------------------------------------------------------------------

# Candidate MCP config paths (in priority order)
_MCP_CONFIG_CANDIDATES: list[Path] = [
    Path.home() / ".mcp.json",
    Path.home() / ".claude" / ".mcp.json",
    Path.home() / ".claude" / "mcp.json",
]


def scan_mcps() -> list[dict[str, Any]]:
    """Read MCP server configs and check liveness via pgrep.

    Reads the first discovered MCP config file.  For each server entry,
    attempts to determine if the server process is currently running.

    Returns:
        List of dicts: {name, healthy, config_path, command}
    """
    config_path, servers = _load_mcp_config()
    if not servers:
        return []

    results: list[dict[str, Any]] = []
    for name, entry in servers.items():
        command: str | None = entry.get("command")
        healthy = _mcp_process_running(name, command)
        results.append({
            "name": name,
            "healthy": healthy,
            "config_path": str(config_path) if config_path else None,
            "command": command,
        })
    return results


def _load_mcp_config() -> tuple[Path | None, dict[str, Any]]:
    """Return (config_path, servers_dict) from the first found MCP config."""
    for candidate in _MCP_CONFIG_CANDIDATES:
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                servers = data.get("mcpServers", {})
                if isinstance(servers, dict):
                    return candidate, servers
            except (ValueError, OSError):
                continue
    return None, {}


def _mcp_process_running(server_name: str, command: str | None) -> bool:
    """Check if an MCP server process is alive via pgrep.

    Searches by server name first, then by command basename if available.
    """
    search_terms: list[str] = [server_name]
    if command:
        search_terms.append(Path(command).name)

    for term in search_terms:
        try:
            proc = subprocess.run(
                ["pgrep", "-f", term],
                capture_output=True,
                timeout=3,
            )
            if proc.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    return False


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

_IDLE_COMMANDS: frozenset[str] = frozenset({"bash", "zsh", "sh", "fish", "dash"})
_BACKEND_COMMANDS: dict[str, str] = {
    "claude": "claude",
    "opencode": "opencode",
}


def scan_sessions(session_name: str = "ccbot") -> list[dict[str, Any]]:
    """List tmux windows in the ccbot session with liveness information.

    Args:
        session_name: tmux session name to inspect (default: "ccbot")

    Returns:
        List of dicts: {name, window_id, alive, backend, idle_seconds}
    """
    windows = _list_tmux_windows(session_name)
    results: list[dict[str, Any]] = []
    for w in windows:
        pane_cmd = w.get("pane_current_command", "")
        alive = pane_cmd not in _IDLE_COMMANDS and bool(pane_cmd)
        backend = _detect_backend_from_command(pane_cmd)
        results.append({
            "name": w.get("window_name", ""),
            "window_id": w.get("window_id", ""),
            "alive": alive,
            "backend": backend,
            "idle_seconds": None,  # tmux doesn't expose this natively without activity tracking
            "pane_command": pane_cmd,
            "cwd": w.get("cwd", ""),
        })
    return results


def _list_tmux_windows(session_name: str) -> list[dict[str, str]]:
    """Return raw window info from tmux list-windows."""
    # Format: window_id|window_name|pane_current_command|pane_current_path
    fmt = "#{window_id}|#{window_name}|#{pane_current_command}|#{pane_current_path}"
    try:
        proc = subprocess.run(
            ["tmux", "list-windows", "-t", session_name, "-F", fmt],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return []
        windows: list[dict[str, str]] = []
        for line in proc.stdout.strip().splitlines():
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            windows.append({
                "window_id": parts[0],
                "window_name": parts[1],
                "pane_current_command": parts[2],
                "cwd": parts[3],
            })
        return windows
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _detect_backend_from_command(pane_cmd: str) -> str | None:
    """Infer backend from the pane's current command name."""
    if not pane_cmd:
        return None
    cmd_lower = pane_cmd.lower()
    for backend, cli in _BACKEND_COMMANDS.items():
        if cli in cmd_lower:
            return backend
    return None


# ---------------------------------------------------------------------------
# Combined scan
# ---------------------------------------------------------------------------

def scan_all(session_name: str = "ccbot") -> dict[str, Any]:
    """Run all scans and return a combined dict.

    Args:
        session_name: tmux session name for scan_sessions()

    Returns:
        {"backends": [...], "models": {...}, "mcps": [...], "sessions": [...]}
    """
    backends = scan_backends()

    # Collect models for every installed backend
    models: dict[str, Any] = {}
    for b in backends:
        if b["installed"]:
            models[b["id"]] = scan_models(b["id"])

    return {
        "backends": backends,
        "models": models,
        "mcps": scan_mcps(),
        "sessions": scan_sessions(session_name),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    session = sys.argv[1] if len(sys.argv) > 1 else "ccbot"
    data = scan_all(session_name=session)
    print(json.dumps(data, indent=2))
