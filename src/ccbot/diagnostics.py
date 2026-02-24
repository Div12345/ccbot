"""Diagnostics module — gathers health/status info from all stack layers.

Layers checked:
  - Bot process: PID, uptime, memory
  - tmux: session status, window list, processes
  - Profiles: active/suspended state
  - Session monitor: tracked sessions, last poll
  - Message queue: pending messages per topic
  - System: disk, CPU throttling, quota
"""

import asyncio
import logging
import os
import time
from pathlib import Path

from .config import config
from .tmux_manager import tmux_manager
from .profiles import profile_manager
from .session import session_manager

logger = logging.getLogger(__name__)

_bot_start_time = time.time()


def _uptime_str() -> str:
    """Human-readable uptime."""
    secs = int(time.time() - _bot_start_time)
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    mins_rem = mins % 60
    return f"{hours}h{mins_rem:02d}m"


async def get_status_summary() -> str:
    """One-shot health check of entire stack. Used by /status command."""
    lines = []

    # --- Bot health ---
    pid = os.getpid()
    uptime = _uptime_str()
    lines.append(f"🤖 *CC Bot*: PID {pid}, up {uptime}")

    # Check if OC bot is also running
    try:
        proc = await asyncio.create_subprocess_exec(
            "pgrep", "-f", "ccbot.*opencode|CCBOT_BACKEND=opencode",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if stdout.strip():
            lines.append("🤖 *OC Bot*: running")
        else:
            lines.append("⚪ *OC Bot*: not running")
    except Exception:
        lines.append("⚪ *OC Bot*: unknown")

    lines.append("")

    # --- Profiles ---
    profiles = profile_manager.list_profiles()
    if profiles:
        lines.append("*Profiles:*")
        for profile, state in profiles:
            is_active = bool(state.window_id)
            indicator = "●" if is_active else "○"

            # Check if the window actually exists (detect stale state)
            status_detail = ""
            if is_active:
                w = await tmux_manager.find_window_by_id(state.window_id)
                if w:
                    # Check what process is running
                    cmd = w.pane_current_command or "unknown"
                    if state.last_active:
                        ago = int(time.time() - state.last_active)
                        if ago < 60:
                            ago_str = "now"
                        elif ago < 3600:
                            ago_str = f"{ago // 60}m ago"
                        else:
                            ago_str = f"{ago // 3600}h ago"
                        status_detail = f" — {cmd}, {ago_str}"
                    else:
                        status_detail = f" — {cmd}"
                else:
                    status_detail = " — ⚠️ window gone"
                    # Auto-fix stale state
                    profile_manager.suspend(profile.slug)
                    indicator = "○"

            backend_info = profile.backend
            if profile.model:
                backend_info += f"/{profile.model}"

            lines.append(f"  {indicator} {profile.icon} {profile.name} `{backend_info}`{status_detail}")

    lines.append("")

    # --- tmux windows (non-profile) ---
    windows = await tmux_manager.list_windows()
    profile_wids = {s.window_id for s in profile_manager.states.values() if s.window_id}
    other_windows = [w for w in windows if w.window_id not in profile_wids]

    if other_windows:
        lines.append(f"*Other windows:* ({len(other_windows)})")
        for w in other_windows[:8]:  # Cap at 8 to not flood
            cmd = w.pane_current_command or "shell"
            lines.append(f"  📺 {w.window_name}: `{cmd}`")
        if len(other_windows) > 8:
            lines.append(f"  ... +{len(other_windows) - 8} more")

    lines.append("")

    # --- System health ---
    # Disk space
    try:
        st = os.statvfs("/home")
        free_gb = (st.f_bavail * st.f_frsize) / (1024**3)
        lines.append(f"💾 Disk: {free_gb:.1f}GB free")
    except Exception:
        pass

    # CPU throttling check
    throttle_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if throttle_path.exists():
        try:
            freq_khz = int(throttle_path.read_text().strip())
            freq_ghz = freq_khz / 1_000_000
            max_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq")
            if max_path.exists():
                max_khz = int(max_path.read_text().strip())
                pct = int(freq_khz / max_khz * 100)
                emoji = "🟢" if pct > 80 else "🟡" if pct > 50 else "🔴"
                lines.append(f"{emoji} CPU: {freq_ghz:.1f}GHz ({pct}% of max)")
            else:
                lines.append(f"⚡ CPU: {freq_ghz:.1f}GHz")
        except Exception:
            pass

    # Load average
    try:
        load1, load5, load15 = os.getloadavg()
        lines.append(f"📈 Load: {load1:.1f} / {load5:.1f} / {load15:.1f}")
    except Exception:
        pass

    # Message queue depth
    from .handlers.message_queue import get_message_queue
    total_queued = 0
    for uid in list(session_manager.thread_bindings.keys()):
        q, _ = get_message_queue(uid)
        total_queued += q.qsize()
    if total_queued > 0:
        lines.append(f"📬 Queue: {total_queued} messages pending")
    else:
        lines.append("📬 Queue: clear")

    # Verbose level
    lines.append(f"🔈 Verbose: {config.verbose_level}")

    return "\n".join(lines)


async def get_ps_output() -> str:
    """Show what's running in each tmux window. Used by /ps command."""
    windows = await tmux_manager.list_windows()
    if not windows:
        return "No tmux windows found."

    lines = ["*Running processes:*", ""]

    for w in windows:
        cmd = w.pane_current_command or "shell"
        cwd_short = w.cwd
        # Shorten common prefixes
        if cwd_short.startswith("/mnt/c/Users/din18/OneDrive - University of Pittsburgh/Work/Github/"):
            cwd_short = "~github/" + cwd_short.split("/Github/")[1]
        elif cwd_short.startswith("/home/div/"):
            cwd_short = "~/" + cwd_short[10:]

        # Check if it's a profile
        profile_tag = ""
        for slug, state in profile_manager.states.items():
            if state.window_id == w.window_id:
                p = profile_manager.profiles.get(slug)
                if p:
                    profile_tag = f" {p.icon}"
                break

        # Check session binding
        bound_to = ""
        for uid, tid, wid in session_manager.iter_thread_bindings():
            if wid == w.window_id:
                bound_to = f" 🔗topic:{tid}"
                break

        # Get session info
        ws = session_manager.window_states.get(w.window_id)
        session_info = ""
        if ws and ws.session_id:
            session_info = f" sid:`{ws.session_id[:8]}…`"

        lines.append(
            f"📺 *{w.window_name}*{profile_tag} (`{w.window_id}`)\n"
            f"   `{cmd}` in `{cwd_short}`{session_info}{bound_to}"
        )
        lines.append("")

    return "\n".join(lines)


async def get_diag_output() -> str:
    """Full diagnostic dump. Used by /diag command."""
    lines = ["*=== FULL DIAGNOSTIC ===*", ""]

    # --- Bot ---
    pid = os.getpid()
    uptime = _uptime_str()
    lines.append(f"*Bot:* PID={pid}, uptime={uptime}, backend={config.backend}")
    lines.append(f"  config\\_dir: `{config.config_dir}`")
    lines.append(f"  verbose: {config.verbose_level}")
    lines.append(f"  tmux\\_session: {config.tmux_session_name}")
    lines.append("")

    # --- tmux ---
    windows = await tmux_manager.list_windows()
    lines.append(f"*tmux:* {len(windows)} windows")
    for w in windows:
        cmd = w.pane_current_command or "shell"
        lines.append(f"  `{w.window_id}` {w.window_name}: {cmd}")
    lines.append("")

    # --- Session monitor state ---
    from .monitor_state import MonitorState
    monitor_state = MonitorState(config.monitor_state_file)
    monitor_state.load()
    tracked = monitor_state.tracked_sessions
    lines.append(f"*Monitor:* {len(tracked)} tracked sessions")
    for sid, info in list(tracked.items())[:5]:
        offset = info.last_byte_offset
        lines.append(f"  `{sid[:12]}…` offset={offset}")
    lines.append("")

    # --- Window states ---
    lines.append(f"*Window states:* {len(session_manager.window_states)}")
    for wid, ws in session_manager.window_states.items():
        sid_short = ws.session_id[:12] + "…" if ws.session_id else "none"
        lines.append(f"  `{wid}` ({ws.window_name}): sid={sid_short}")
    lines.append("")

    # --- Thread bindings ---
    bindings = list(session_manager.iter_thread_bindings())
    lines.append(f"*Thread bindings:* {len(bindings)}")
    for uid, tid, wid in bindings:
        display = session_manager.get_display_name(wid)
        lines.append(f"  user={uid}, topic={tid} → `{wid}` ({display})")
    lines.append("")

    # --- Profile states ---
    lines.append(f"*Profiles:* {len(profile_manager.profiles)} defined")
    for slug, profile in profile_manager.profiles.items():
        state = profile_manager.get_state(slug)
        active = "ACTIVE" if state.window_id else "suspended"
        lines.append(
            f"  {profile.icon} {slug}: {active}"
            f"{f', wid={state.window_id}' if state.window_id else ''}"
            f"{f', topic={state.topic_id}' if state.topic_id else ''}"
            f"{f', last_sid={state.last_session_id[:8]}…' if state.last_session_id else ''}"
        )
    lines.append("")

    # --- Message queues ---
    from .handlers.message_queue import get_message_queue
    lines.append("*Message queues:*")
    has_queues = False
    for uid in list(session_manager.thread_bindings.keys()):
        q, _ = get_message_queue(uid)
        sz = q.qsize()
        if sz > 0:
            lines.append(f"  user={uid}: {sz} pending")
            has_queues = True
    if not has_queues:
        lines.append("  All clear")
    lines.append("")

    # --- Group chat IDs ---
    lines.append(f"*Group chat IDs:* {len(session_manager.group_chat_ids)}")
    for key, cid in session_manager.group_chat_ids.items():
        lines.append(f"  {key} → {cid}")
    lines.append("")

    # --- System ---
    try:
        load1, load5, _ = os.getloadavg()
        lines.append(f"*System:* load={load1:.1f}/{load5:.1f}")
    except Exception:
        pass

    try:
        st = os.statvfs("/home")
        free_gb = (st.f_bavail * st.f_frsize) / (1024**3)
        lines.append(f"  disk: {free_gb:.1f}GB free")
    except Exception:
        pass

    # Memory of this process
    try:
        import resource
        mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        lines.append(f"  bot memory: {mem_mb:.0f}MB")
    except Exception:
        pass

    return "\n".join(lines)


async def get_alive_check(window_id: str) -> str:
    """Check if a specific window's CLI process is responsive."""
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return f"❌ Window `{window_id}` not found"

    cmd = w.pane_current_command or "unknown"

    # Check if it's a known CLI tool
    is_cli = cmd in ("claude", "opencode", "node", "python3")

    # Check last JSONL write time for this window's session
    ws = session_manager.window_states.get(window_id)
    jsonl_age = ""
    if ws and ws.session_id and ws.cwd:
        file_path = session_manager._build_session_file_path(ws.session_id, ws.cwd)
        if file_path and file_path.exists():
            age_secs = int(time.time() - file_path.stat().st_mtime)
            if age_secs < 5:
                jsonl_age = "✅ writing now"
            elif age_secs < 30:
                jsonl_age = f"✅ wrote {age_secs}s ago"
            elif age_secs < 120:
                jsonl_age = f"🟡 last write {age_secs}s ago"
            else:
                mins = age_secs // 60
                jsonl_age = f"🔴 idle {mins}m"

    name = w.window_name
    status = "running" if is_cli else f"process: {cmd}"
    return f"📺 *{name}* (`{window_id}`): {status}\n   {jsonl_age}" if jsonl_age else f"📺 *{name}* (`{window_id}`): {status}"
