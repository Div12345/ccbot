"""Telegram bot handlers — the main UI layer of CCBot.

Registers all command/callback/message handlers and manages the bot lifecycle.
Each Telegram topic maps 1:1 to a tmux window (Claude session).

Core responsibilities:
  - Command handlers: /start, /history, /screenshot, /esc, /kill, /unbind,
    plus forwarding unknown /commands to Claude Code via tmux.
  - Callback query handler: directory browser, history pagination,
    interactive UI navigation, screenshot refresh.
  - Topic-based routing: each named topic binds to one tmux window.
    Unbound topics trigger the directory browser to create a new session.
  - Photo handling: photos sent by user are downloaded and forwarded
    to Claude Code as file paths (photo_handler).
  - Automatic cleanup: closing a topic kills the associated window
    (topic_closed_handler). Unsupported content (stickers, voice, etc.)
    is rejected with a warning (unsupported_content_handler).
  - Bot lifecycle management: post_init, post_shutdown, create_bot.

Handler modules (in handlers/):
  - callback_data: Callback data constants
  - message_queue: Per-user message queue management
  - message_sender: Safe message sending helpers
  - history: Message history pagination
  - directory_browser: Directory browser UI
  - interactive_ui: Interactive UI handling
  - status_polling: Terminal status polling
  - response_builder: Response message building

Key functions: create_bot(), handle_new_message().
"""

import asyncio
import io
import logging
import time
from pathlib import Path

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import config
from .diagnostics import get_status_summary, get_ps_output, get_diag_output, get_alive_check
from .profiles import profile_manager
from .handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
    CB_KEYS_PREFIX,
    CB_LB_BACKEND,
    CB_LB_DIR,
    CB_LB_DIR_BROWSE,
    CB_LB_FLAG,
    CB_LB_GO,
    CB_LB_MODEL,
    CB_LB_PROFILE,
    CB_LB_SAVE,
    CB_PROFILE_INFO,
    CB_PROFILE_LAUNCH,
    CB_PROFILE_SUSPEND,
    CB_SCREENSHOT_REFRESH,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
)
from .handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
    build_directory_browser,
    build_window_picker,
    clear_browse_state,
    clear_window_picker_state,
)
from .handlers.cleanup import clear_topic_state
from .handlers.history import send_history
from .handlers.interactive_ui import (
    INTERACTIVE_TOOL_NAMES,
    clear_interactive_mode,
    clear_interactive_msg,
    get_interactive_msg_id,
    get_interactive_window,
    handle_interactive_ui,
    set_interactive_mode,
)
from .handlers.message_queue import (
    clear_status_msg_info,
    enqueue_content_message,
    enqueue_status_update,
    get_message_queue,
    shutdown_workers,
)
from .handlers.message_sender import (
    NO_LINK_PREVIEW,
    safe_edit,
    safe_reply,
    safe_send,
    send_with_fallback,
)
from .markdown_v2 import convert_markdown
from .handlers.response_builder import build_response_parts
from .handlers.status_polling import status_poll_loop
from .screenshot import text_to_image
from .session import session_manager
from .session_monitor import NewMessage, SessionMonitor
from .opencode_monitor import OpenCodeMonitor
from .terminal_parser import extract_bash_output
from .tmux_manager import tmux_manager
from .utils import ccbot_dir

logger = logging.getLogger(__name__)

# Session monitor instance
session_monitor: SessionMonitor | OpenCodeMonitor | None = None

# Status polling task
_status_poll_task: asyncio.Task | None = None

# Claude Code slash commands — forwarded to the CLI when used in a topic.
# Organized by category for /cmds searchability.
CC_COMMANDS: dict[str, str] = {
    # Session
    "clear": "↗ Clear conversation history",
    "compact": "↗ Compact conversation context",
    "resume": "↗ Resume a previous session",
    # Model & config
    "model": "↗ Switch AI model",
    "permissions": "↗ Manage tool permissions",
    # Info
    "cost": "↗ Show token/cost usage",
    "context": "↗ Show context usage grid",
    # Files
    "memory": "↗ Edit CLAUDE.md",
    # Hooks
    "hooks": "↗ Show active hooks",
}

# Extended command descriptions for /cmds search (not registered as BotCommands
# to keep the Telegram menu clean, but shown in /cmds and forwarded on use)
CC_COMMANDS_EXTENDED: dict[str, str] = {
    **CC_COMMANDS,
    # Additional commands that Claude Code supports
    "init": "↗ Initialize project with CLAUDE.md",
    "bug": "↗ Report a bug",
    "terminal-setup": "↗ Configure terminal",
    "doctor": "↗ Diagnose Claude Code issues",
    "login": "↗ Authentication",
    "logout": "↗ Log out",
    "config": "↗ Claude Code configuration",
    "mcp": "↗ MCP server management",
    "listen": "↗ Listen for notifications",
    "review": "↗ Code review mode",
    "pr-review": "↗ Review a pull request",
}

# Categorized for /cmds display
CC_COMMAND_CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "Session": [
        ("/clear", "Clear conversation history"),
        ("/compact", "Compact conversation context"),
        ("/resume", "Resume a previous session"),
    ],
    "Model & Config": [
        ("/model", "Switch AI model"),
        ("/permissions", "Manage tool permissions"),
        ("/config", "Claude Code configuration"),
        ("/mcp", "MCP server management"),
        ("/hooks", "Show active hooks"),
    ],
    "Info": [
        ("/cost", "Token/cost usage"),
        ("/context", "Context usage grid"),
        ("/doctor", "Diagnose issues"),
    ],
    "Files": [
        ("/memory", "Edit CLAUDE.md"),
        ("/init", "Initialize project CLAUDE.md"),
    ],
    "Code": [
        ("/review", "Code review mode"),
        ("/pr-review", "Review a pull request"),
    ],
}

# CCBot's own commands, categorized for /cmds
CCBOT_COMMAND_CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "Monitor": [
        ("/status", "Health check of entire stack"),
        ("/alive", "Is Claude still writing?"),
        ("/ps", "All tmux windows + processes"),
        ("/diag", "Full diagnostic dump"),
        ("/usage", "Quota remaining"),
    ],
    "Workspaces": [
        ("/launch", "Interactive session launcher (pick backend/model/dir)"),
        ("/profiles", "Launch/suspend workspace profiles"),
        ("/screenshot", "Terminal screenshot + controls"),
        ("/history", "Message history for topic"),
    ],
    "Control": [
        ("/esc", "Send Escape to interrupt"),
        ("/relaunch", "Exit + reopen Claude (reload MCP etc)"),
        ("/kill", "Kill session + delete topic"),
        ("/unbind", "Detach topic from session"),
        ("/flush", "Clear message backlog"),
        ("/verbose", "Set noise level (0/1/2)"),
    ],
    "Help": [
        ("/help", "This command reference"),
        ("/cmds", "Searchable command list"),
    ],
}


def is_user_allowed(user_id: int | None) -> bool:
    return user_id is not None and config.is_user_allowed(user_id)


def _get_thread_id(update: Update) -> int | None:
    """Extract thread_id from an update, returning None if not in a named topic."""
    msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None or tid == 1:
        return None
    return tid


# --- Command handlers ---


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    clear_browse_state(context.user_data)

    if update.message:
        await safe_reply(
            update.message,
            "🤖 *Claude Code Monitor*\n\n"
            "Each topic is a session. Create a new topic to start.\n"
            "Type /help for all commands.",
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Smart help — organized by what you need to do."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    help_text = (
        "📖 *Command Reference*\n"
        "\n"
        "*What's happening?*\n"
        "  /status — quick health check (bot, profiles, system)\n"
        "  /alive — is Claude still writing? (checks JSONL freshness)\n"
        "  /ps — all tmux windows + what's running in each\n"
        "  /diag — full diagnostic dump (when something's broken)\n"
        "  /usage — Claude Code quota remaining\n"
        "\n"
        "*Workspaces:*\n"
        "  /profiles — launch/suspend/manage saved workspaces\n"
        "  /screenshot — see the terminal + control keys\n"
        "  /history — message history for this topic\n"
        "\n"
        "*Control:*\n"
        "  /esc — send Escape (interrupt Claude)\n"
        "  /kill — kill session + delete topic\n"
        "  /unbind — detach topic from session (keeps window)\n"
        "  /flush — clear message backlog instantly\n"
        "  /verbose 0|1|2 — noise level (0=quiet, 1=normal, 2=all)\n"
        "\n"
        "*Quick guide:*\n"
        "  Slow/no messages? → /alive then /flush\n"
        "  Backlog flooding? → /flush then /verbose 0\n"
        "  Something broken? → /diag\n"
        "  Start a workspace? → /profiles\n"
        "  Need fresh session? → create a new topic\n"
        "\n"
        "Any /slash command not listed here gets forwarded to Claude Code."
    )

    if update.message:
        await safe_reply(update.message, help_text)


async def cmds_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Searchable command list — /cmds or /cmds <search term>."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    # Parse search query
    args = update.message.text.split(maxsplit=1)
    search = args[1].lower().strip() if len(args) > 1 else ""

    lines = []

    if search:
        lines.append(f"*Commands matching \"{search}\":*\n")
        found = False
        # Search CCBot commands
        for cat, cmds in CCBOT_COMMAND_CATEGORIES.items():
            matches = [(c, d) for c, d in cmds if search in c.lower() or search in d.lower()]
            if matches:
                found = True
                lines.append(f"*{cat}* (ccbot):")
                for cmd, desc in matches:
                    lines.append(f"  `{cmd}` — {desc}")
                lines.append("")
        # Search CC commands
        for cat, cmds in CC_COMMAND_CATEGORIES.items():
            matches = [(c, d) for c, d in cmds if search in c.lower() or search in d.lower()]
            if matches:
                found = True
                lines.append(f"*{cat}* (↗ Claude Code):")
                for cmd, desc in matches:
                    lines.append(f"  `{cmd}` — {desc}")
                lines.append("")
        if not found:
            lines.append("No commands found. Try a broader search.")
    else:
        lines.append("*All Commands*\n")
        lines.append("*— CCBot (runs here) —*\n")
        for cat, cmds in CCBOT_COMMAND_CATEGORIES.items():
            lines.append(f"*{cat}:*")
            for cmd, desc in cmds:
                lines.append(f"  `{cmd}` — {desc}")
            lines.append("")
        lines.append("*— Claude Code (↗ forwarded) —*\n")
        for cat, cmds in CC_COMMAND_CATEGORIES.items():
            lines.append(f"*{cat}:*")
            for cmd, desc in cmds:
                lines.append(f"  `{cmd}` — {desc}")
            lines.append("")
        lines.append("💡 `/cmds <word>` to search")

    await safe_reply(update.message, "\n".join(lines))


async def sessionconfig_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current session/profile configuration for this topic."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    lines = []

    # Check if this topic is bound to a profile
    profile = None
    profile_state = None
    if thread_id:
        profile = profile_manager.find_by_topic(thread_id)
        if profile:
            profile_state = profile_manager.get_state(profile.slug)

    if profile:
        lines.append(f"{profile.icon} *{profile.name}* config:\n")
        lines.append(f"  Dir: `{profile.directory}`")
        lines.append(f"  Backend: `{profile.backend}`")
        lines.append(f"  Model: `{profile.model or 'default'}`")
        lines.append(f"  Flags: `{profile.flags or 'none'}`")
        lines.append(f"  Resume: {'yes' if profile.resume else 'no'}")
        lines.append(f"  Max idle: {profile.max_idle_minutes}m")
        if profile.system_prompt:
            prompt_preview = profile.system_prompt[:80].replace('\n', ' ')
            lines.append(f"  Prompt: _{prompt_preview}..._")
        if profile.obsidian_note:
            lines.append(f"  Obsidian: `{profile.obsidian_note}`")
        lines.append("")

        # Runtime state
        if profile_state:
            if profile_state.window_id:
                lines.append(f"  Window: `{profile_state.window_id}`")
            if profile_state.last_session_id:
                lines.append(f"  Session: `{profile_state.last_session_id[:16]}...`")
            deep_link = profile_manager.get_telegram_deep_link(profile.slug)
            if deep_link:
                lines.append(f"  [Deep link]({deep_link})")
    else:
        # Not a profile topic — show generic session info
        lines.append("*Session config:*\n")

        if thread_id:
            wid = session_manager.get_window_for_thread(user.id, thread_id)
            if wid:
                ws = session_manager.window_states.get(wid)
                display = session_manager.get_display_name(wid)
                lines.append(f"  Window: `{wid}` ({display})")
                if ws:
                    if ws.cwd:
                        lines.append(f"  Dir: `{ws.cwd}`")
                    if ws.session_id:
                        lines.append(f"  Session: `{ws.session_id[:16]}...`")

                w = await tmux_manager.find_window_by_id(wid)
                if w:
                    lines.append(f"  Process: `{w.pane_current_command or 'unknown'}`")
            else:
                lines.append("  No window bound to this topic.")
        else:
            lines.append("  Not in a topic. Use /profiles to launch a workspace.")

    lines.append("")
    lines.append(f"  Verbose: {config.verbose_level}")
    lines.append(f"  Backend: `{config.backend}`")

    lines.append("\n💡 To change model: `/model` (forwarded to Claude Code)")
    lines.append("💡 To change verbose: `/verbose 0|1|2`")

    await safe_reply(update.message, "\n".join(lines))


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show message history for the active session or bound thread."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    await send_history(update.message, wid)


async def screenshot_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Capture the current tmux pane and send it as an image."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
    if not text:
        await safe_reply(update.message, "❌ Failed to capture pane content.")
        return

    png_bytes = await text_to_image(text, with_ansi=True)
    keyboard = _build_screenshot_keyboard(wid)
    await update.message.reply_document(
        document=io.BytesIO(png_bytes),
        filename="screenshot.png",
        reply_markup=keyboard,
    )


async def unbind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unbind this topic from its Claude session without killing the window."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ This command only works in a topic.")
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    display = session_manager.get_display_name(wid)
    session_manager.unbind_thread(user.id, thread_id)
    await clear_topic_state(user.id, thread_id, context.bot, context.user_data)

    await safe_reply(
        update.message,
        f"✅ Topic unbound from window '{display}'.\n"
        "The Claude session is still running in tmux.\n"
        "Send a message to bind to a new session.",
    )


async def esc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Escape key to interrupt Claude."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    # Send Escape control character (no enter)
    await tmux_manager.send_keys(w.window_id, "\x1b", enter=False)
    await safe_reply(update.message, "⎋ Sent Escape")


async def verbose_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set content verbosity: /verbose [0|1|2]."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    args = (update.message.text or "").split()
    labels = {0: "Quiet (text only)", 1: "Normal (tool icons)", 2: "Verbose (everything)"}

    if len(args) > 1 and args[1] in ("0", "1", "2"):
        config.verbose_level = int(args[1])
        await safe_reply(update.message, f"Verbosity: {labels[config.verbose_level]}")
    else:
        await safe_reply(
            update.message,
            f"Current: {config.verbose_level} — {labels[config.verbose_level]}\n"
            "Usage: /verbose 0 | 1 | 2",
        )


async def flush_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Flush message backlog for this topic."""
    import os as _os

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    from .handlers.message_queue import flush_queue

    # 1. Drain the message queue for this thread
    dropped = await flush_queue(user.id, thread_id)

    # 2. Advance monitor read cursor to EOF
    skipped_bytes = 0
    if session_monitor:
        wid = session_manager.get_window_for_thread(user.id, thread_id)
        if wid:
            ws = session_manager.window_states.get(wid)
            if ws and ws.session_id:
                tracked = session_monitor.state.get_session(ws.session_id)
                if tracked and tracked.file_path:
                    try:
                        file_size = _os.path.getsize(tracked.file_path)
                        skipped_bytes = max(0, file_size - tracked.last_byte_offset)
                        tracked.last_byte_offset = file_size
                        session_monitor.state.save()
                    except OSError:
                        pass

    parts = []
    if dropped:
        parts.append(f"{dropped} queued msgs dropped")
    if skipped_bytes:
        parts.append(f"{skipped_bytes // 1024}KB monitor backlog skipped")
    if not parts:
        parts.append("nothing to flush")
    await safe_reply(update.message, f"Flush: {', '.join(parts)}.")


async def profiles_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show profile picker as inline keyboard grid."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    profiles = profile_manager.list_profiles()
    if not profiles:
        if update.message:
            await safe_reply(
                update.message,
                "No profiles configured.\n"
                "Create profiles in `~/.ccbot/profiles/` as JSON files.\n\n"
                "Example (`~/.ccbot/profiles/arterial.json`):\n"
                "```\n"
                "{\n"
                '  "name": "Arterial Analysis",\n'
                '  "icon": "🧪",\n'
                '  "directory": "/path/to/project",\n'
                '  "backend": "claude",\n'
                '  "flags": "--dangerously-skip-permissions"\n'
                "}\n"
                "```",
            )
        return

    # Build inline keyboard: 2 columns, icon + name, ● for active ○ for suspended
    buttons = []
    row = []
    for profile, state in profiles:
        is_active = bool(state.window_id)
        indicator = "●" if is_active else "○"
        label = f"{profile.icon} {profile.name} {indicator}"
        callback = f"pf:launch:{profile.slug}" if not is_active else f"pf:info:{profile.slug}"
        row.append(InlineKeyboardButton(label, callback_data=callback))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    keyboard = InlineKeyboardMarkup(buttons)
    active = profile_manager.active_count()
    if update.message:
        await safe_reply(
            update.message,
            f"📋 *Profiles* ({active}/{profile_manager.MAX_ACTIVE} active)\n"
            "● = live  ○ = suspended\n"
            "Tap to launch or view info.",
            reply_markup=keyboard,
        )


# --- Launch builder helpers ---

def _lb_key(thread_id: int | None) -> str:
    """User-data key for the launch builder state."""
    return f"lb:{thread_id or 0}"


def _get_known_dirs() -> list[str]:
    """Collect known directories from profiles and active sessions."""
    dirs: dict[str, bool] = {}
    for prof in profile_manager.profiles.values():
        if prof.directory:
            dirs[prof.directory] = True
    for ws in session_manager.window_states.values():
        if ws.cwd:
            dirs[ws.cwd] = True
    return list(dirs.keys())


def _short_dir(path: str) -> str:
    """Abbreviate a directory path for display."""
    import os
    path = path.replace(os.path.expanduser("~"), "~")
    # Shorten Windows OneDrive paths
    if "/OneDrive" in path and "/Github/" in path:
        return path.split("/Github/")[-1]
    if len(path) > 35:
        return "…/" + Path(path).name
    return path


def _build_lb_message(lb: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Render the current builder state as text + inline keyboard."""
    step = lb.get("step", "main")
    backend = lb.get("backend", "")
    model = lb.get("model", "")
    dir_idx = lb.get("dir_idx", -1)
    known_dirs = lb.get("known_dirs", [])
    skip_perms = lb.get("skip_perms", True)
    resume = lb.get("resume", True)

    lines = ["⚙️ *Launch Session Builder*", ""]

    # Show choices made so far
    if backend:
        lines.append(f"Backend: `{backend}` ✓")
    if model:
        lines.append(f"Model: `{model}` ✓")
    elif backend:
        lines.append("Model: `default` ✓")
    if dir_idx >= 0 and dir_idx < len(known_dirs):
        lines.append(f"Dir: `{_short_dir(known_dirs[dir_idx])}` ✓")

    buttons: list[list[InlineKeyboardButton]] = []

    if step == "main":
        # Show saved profiles as quick-launch + custom build
        lines.append("")
        lines.append("Quick launch or build custom:")
        profiles = profile_manager.list_profiles()
        row: list[InlineKeyboardButton] = []
        for prof, state in profiles:
            indicator = "●" if state.window_id else ""
            label = f"{prof.icon} {prof.name} {indicator}"
            row.append(InlineKeyboardButton(label, callback_data=f"{CB_LB_PROFILE}{prof.slug}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("✨ Custom build…", callback_data=f"{CB_LB_BACKEND}pick")])

    elif step == "backend":
        lines.append("")
        lines.append("Pick backend:")
        buttons.append([
            InlineKeyboardButton("Claude", callback_data=f"{CB_LB_BACKEND}claude"),
            InlineKeyboardButton("OpenCode", callback_data=f"{CB_LB_BACKEND}opencode"),
        ])

    elif step == "model":
        lines.append("")
        lines.append("Pick model:")
        if backend == "claude":
            buttons.append([
                InlineKeyboardButton("Default", callback_data=f"{CB_LB_MODEL}default"),
                InlineKeyboardButton("Opus", callback_data=f"{CB_LB_MODEL}opus"),
            ])
            buttons.append([
                InlineKeyboardButton("Sonnet", callback_data=f"{CB_LB_MODEL}sonnet"),
                InlineKeyboardButton("Haiku", callback_data=f"{CB_LB_MODEL}haiku"),
            ])
        else:
            buttons.append([
                InlineKeyboardButton("Default", callback_data=f"{CB_LB_MODEL}default"),
            ])

    elif step == "dir":
        lines.append("")
        lines.append("Pick directory:")
        for i, d in enumerate(known_dirs[:6]):
            buttons.append([InlineKeyboardButton(
                f"📂 {_short_dir(d)}",
                callback_data=f"{CB_LB_DIR}{i}",
            )])

    elif step == "flags":
        lines.append("")
        sp_icon = "✅" if skip_perms else "☐"
        rs_icon = "✅" if resume else "☐"
        buttons.append([
            InlineKeyboardButton(f"{sp_icon} Skip permissions", callback_data=f"{CB_LB_FLAG}skip"),
            InlineKeyboardButton(f"{rs_icon} Resume last", callback_data=f"{CB_LB_FLAG}resume"),
        ])
        buttons.append([
            InlineKeyboardButton("🚀 Launch!", callback_data=CB_LB_GO),
        ])

    text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup(buttons) if buttons else InlineKeyboardMarkup([])
    return text, keyboard


async def launch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Interactive session launcher — pick backend, model, directory, then go."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    known_dirs = _get_known_dirs()

    lb = {
        "step": "main",
        "backend": "",
        "model": "",
        "dir_idx": -1,
        "known_dirs": known_dirs,
        "skip_perms": True,
        "resume": True,
    }

    if context.user_data is not None:
        context.user_data[_lb_key(thread_id)] = lb

    text, keyboard = _build_lb_message(lb)
    await safe_reply(update.message, text, reply_markup=keyboard)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One-shot health check of entire stack."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    summary = await get_status_summary()
    await safe_reply(update.message, summary)


async def ps_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show what's running in each tmux window."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    output = await get_ps_output()
    await safe_reply(update.message, output)


async def diag_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Full diagnostic dump of all stack layers."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    output = await get_diag_output()
    await safe_reply(update.message, output)


async def alive_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check if a session's CLI process is still active/writing."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    thread_id = _get_thread_id(update)

    # If in a topic, check that topic's window
    if thread_id:
        wid = session_manager.get_window_for_thread(user.id, thread_id)
        if wid:
            result = await get_alive_check(wid)
            await safe_reply(update.message, result)
            return

    # No topic context — check all active profile windows
    results = []
    for slug, state in profile_manager.states.items():
        if state.window_id:
            result = await get_alive_check(state.window_id)
            results.append(result)

    if not results:
        # Fall back to all windows
        windows = await tmux_manager.list_windows()
        for w in windows[:6]:
            result = await get_alive_check(w.window_id)
            results.append(result)

    if results:
        await safe_reply(update.message, "\n\n".join(results))
    else:
        await safe_reply(update.message, "No active sessions found.")


async def relaunch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exit and reopen Claude in the same window. Useful for MCP config reloading."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    # Gather info before killing
    ws = session_manager.window_states.get(wid)
    work_dir = ws.cwd if ws else ""
    session_id = ws.session_id if ws else ""
    display_name = session_manager.get_display_name(wid)

    # Check if this topic has a profile
    profile = profile_manager.find_by_topic(thread_id) if thread_id else None

    # Kill the old window
    w = await tmux_manager.find_window_by_id(wid)
    if w:
        await tmux_manager.kill_window(wid)

    # Build the CLI command
    if profile:
        cmd_parts = [
            profile.backend if profile.backend != "claude" else config.claude_command
        ]
        if profile.model:
            cmd_parts.append(f"--model {profile.model}")
        if profile.flags:
            cmd_parts.append(profile.flags)
        if profile.resume and session_id:
            cmd_parts.append(f"--resume {session_id}")
        cli_command = " ".join(cmd_parts)
        work_dir = profile.directory or work_dir
        window_name = profile.slug
    else:
        cmd_parts = [config.claude_command, "--dangerously-skip-permissions"]
        if session_id:
            cmd_parts.append(f"--resume {session_id}")
        cli_command = " ".join(cmd_parts)
        window_name = display_name or None

    if not work_dir:
        await safe_reply(update.message, "❌ Cannot determine working directory.")
        return

    # Create new window
    success, msg, wname, new_wid = await tmux_manager.create_window(
        work_dir=work_dir,
        window_name=window_name,
        start_claude=False,
    )

    if not success:
        await safe_reply(update.message, f"❌ Relaunch failed: {msg}")
        return

    # Send the CLI command
    await tmux_manager.send_keys(new_wid, cli_command, enter=True, literal=True)

    # Re-bind the topic to the new window
    if thread_id:
        session_manager.unbind_thread(user.id, thread_id)
        session_manager.bind_thread(user.id, thread_id, new_wid, window_name=window_name)
        session_manager.window_display_names[new_wid] = display_name or window_name or ""
        session_manager._save_state()

    # Update profile state
    if profile:
        profile_manager.activate(
            profile.slug, window_id=new_wid, topic_id=thread_id or 0
        )

    resume_note = " (resuming session)" if session_id else ""
    await safe_reply(
        update.message,
        f"🔄 Relaunched *{display_name or window_name}*{resume_note}\n📂 `{work_dir}`",
    )


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch Claude Code usage stats from TUI and send to Telegram."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        await safe_reply(update.message, f"Window '{wid}' no longer exists.")
        return

    # Send /usage command to Claude Code TUI
    await tmux_manager.send_keys(w.window_id, "/usage")
    # Wait for the modal to render
    await asyncio.sleep(2.0)
    # Capture the pane content
    pane_text = await tmux_manager.capture_pane(w.window_id)
    # Dismiss the modal
    await tmux_manager.send_keys(w.window_id, "Escape", enter=False, literal=False)

    if not pane_text:
        await safe_reply(update.message, "Failed to capture usage info.")
        return

    # Try to parse structured usage info
    from .terminal_parser import parse_usage_output

    usage = parse_usage_output(pane_text)
    if usage and usage.parsed_lines:
        text = "\n".join(usage.parsed_lines)
        await safe_reply(update.message, f"```\n{text}\n```")
    else:
        # Fallback: send raw pane capture trimmed
        trimmed = pane_text.strip()
        if len(trimmed) > 3000:
            trimmed = trimmed[:3000] + "\n... (truncated)"
        await safe_reply(update.message, f"```\n{trimmed}\n```")


# --- Screenshot keyboard with quick control keys ---

# key_id → (tmux_key, enter, literal)
_KEYS_SEND_MAP: dict[str, tuple[str, bool, bool]] = {
    "up": ("Up", False, False),
    "dn": ("Down", False, False),
    "lt": ("Left", False, False),
    "rt": ("Right", False, False),
    "esc": ("Escape", False, False),
    "ent": ("Enter", False, False),
    "spc": ("Space", False, False),
    "tab": ("Tab", False, False),
    "cc": ("C-c", False, False),
}

# key_id → display label (shown in callback answer toast)
_KEY_LABELS: dict[str, str] = {
    "up": "↑",
    "dn": "↓",
    "lt": "←",
    "rt": "→",
    "esc": "⎋ Esc",
    "ent": "⏎ Enter",
    "spc": "␣ Space",
    "tab": "⇥ Tab",
    "cc": "^C",
}


def _build_screenshot_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for screenshot: control keys + refresh."""

    def btn(label: str, key_id: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label,
            callback_data=f"{CB_KEYS_PREFIX}{key_id}:{window_id}"[:64],
        )

    return InlineKeyboardMarkup(
        [
            [btn("␣ Space", "spc"), btn("↑", "up"), btn("⇥ Tab", "tab")],
            [btn("←", "lt"), btn("↓", "dn"), btn("→", "rt")],
            [btn("⎋ Esc", "esc"), btn("^C", "cc"), btn("⏎ Enter", "ent")],
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"{CB_SCREENSHOT_REFRESH}{window_id}"[:64],
                )
            ],
        ]
    )


async def topic_closed_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic closure — kill the associated tmux window and clean up state."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid:
        display = session_manager.get_display_name(wid)
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            await tmux_manager.kill_window(w.window_id)
            logger.info(
                "Topic closed: killed window %s (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        else:
            logger.info(
                "Topic closed: window %s already gone (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        session_manager.unbind_thread(user.id, thread_id)
        # Clean up all memory state for this topic
        await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
    else:
        logger.debug(
            "Topic closed: no binding (user=%d, thread=%d)", user.id, thread_id
        )


async def forward_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward any non-bot command as a slash command to the active Claude Code session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    cmd_text = update.message.text or ""
    # The full text is already a slash command like "/clear" or "/compact foo"
    cc_slash = cmd_text.split("@")[0]  # strip bot mention
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    display = session_manager.get_display_name(wid)
    logger.info(
        "Forwarding command %s to window %s (user=%d)", cc_slash, display, user.id
    )
    await update.message.chat.send_action(ChatAction.TYPING)
    success, message = await session_manager.send_to_window(wid, cc_slash)
    if success:
        await safe_reply(update.message, f"⚡ [{display}] Sent: {cc_slash}")
        # If /clear command was sent, clear the session association
        # so we can detect the new session after first message
        if cc_slash.strip().lower() == "/clear":
            logger.info("Clearing session for window %s after /clear", display)
            session_manager.clear_window_session(wid)

        # Interactive commands (e.g. /model) render a terminal-based UI
        # with no JSONL tool_use entry.  The status poller already detects
        # interactive UIs every 1s (status_polling.py), so no
        # proactive detection needed here — the poller handles it.
    else:
        await safe_reply(update.message, f"❌ {message}")


async def unsupported_content_handler(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reply to non-text messages (images, stickers, voice, etc.)."""
    if not update.message:
        return
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    logger.debug("Unsupported content from user %d", user.id)
    await safe_reply(
        update.message,
        "⚠ Only text messages are supported. Images, stickers, voice, and other media cannot be forwarded to Claude Code.",
    )


# --- Image directory for incoming photos ---
_IMAGES_DIR = ccbot_dir() / "images"
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent by the user: download and forward path to Claude Code."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.photo:
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No session bound to this topic. Send a text message first to create one.",
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        session_manager.unbind_thread(user.id, thread_id)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    # Download the highest-resolution photo
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()

    # Save to ~/.ccbot/images/<timestamp>_<file_unique_id>.jpg
    filename = f"{int(time.time())}_{photo.file_unique_id}.jpg"
    file_path = _IMAGES_DIR / filename
    await tg_file.download_to_drive(file_path)

    # Build the message to send to Claude Code
    caption = update.message.caption or ""
    if caption:
        text_to_send = f"{caption}\n\n(image attached: {file_path})"
    else:
        text_to_send = f"(image attached: {file_path})"

    await update.message.chat.send_action(ChatAction.TYPING)
    clear_status_msg_info(user.id, thread_id)

    success, message = await session_manager.send_to_window(wid, text_to_send)
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    # Confirm to user
    await safe_reply(update.message, "📷 Image sent to Claude Code.")


# Active bash capture tasks: (user_id, thread_id) → asyncio.Task
_bash_capture_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}


def _cancel_bash_capture(user_id: int, thread_id: int) -> None:
    """Cancel any running bash capture for this topic."""
    key = (user_id, thread_id)
    task = _bash_capture_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def _capture_bash_output(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    command: str,
) -> None:
    """Background task: capture ``!`` bash command output from tmux pane.

    Sends the first captured output as a new message, then edits it
    in-place as more output appears.  Stops after 30 s or when cancelled
    (e.g. user sends a new message, which pushes content down).
    """
    try:
        # Wait for the command to start producing output
        await asyncio.sleep(2.0)

        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        msg_id: int | None = None
        last_output: str = ""

        for _ in range(30):
            raw = await tmux_manager.capture_pane(window_id)
            if raw is None:
                return

            output = extract_bash_output(raw, command)
            if not output:
                await asyncio.sleep(1.0)
                continue

            # Skip edit if nothing changed
            if output == last_output:
                await asyncio.sleep(1.0)
                continue

            last_output = output

            # Truncate to fit Telegram's 4096-char limit
            if len(output) > 3800:
                output = "… " + output[-3800:]

            if msg_id is None:
                # First capture — send a new message
                sent = await send_with_fallback(
                    bot,
                    chat_id,
                    output,
                    message_thread_id=thread_id,
                )
                if sent:
                    msg_id = sent.message_id
            else:
                # Subsequent captures — edit in place
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=convert_markdown(output),
                        parse_mode="MarkdownV2",
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=output,
                            link_preview_options=NO_LINK_PREVIEW,
                        )
                    except Exception:
                        pass

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return
    finally:
        _bash_capture_tasks.pop((user_id, thread_id), None)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.text:
        return

    thread_id = _get_thread_id(update)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    text = update.message.text

    # Ignore text in window picker mode (only for the same thread)
    if context.user_data and context.user_data.get(STATE_KEY) == STATE_SELECTING_WINDOW:
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the window picker above, or tap Cancel.",
            )
            return
        # Stale picker state from a different thread — clear it
        clear_window_picker_state(context.user_data)
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)

    # Ignore text in directory browsing mode (only for the same thread)
    if (
        context.user_data
        and context.user_data.get(STATE_KEY) == STATE_BROWSING_DIRECTORY
    ):
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the directory browser above, or tap Cancel.",
            )
            return
        # Stale browsing state from a different thread — clear it
        clear_browse_state(context.user_data)
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        # Unbound topic — check for unbound windows first
        all_windows = await tmux_manager.list_windows()
        bound_ids = {wid for _, _, wid in session_manager.iter_thread_bindings()}
        unbound = [
            (w.window_id, w.window_name, w.cwd)
            for w in all_windows
            if w.window_id not in bound_ids
        ]
        logger.debug(
            "Window picker check: all=%s, bound=%s, unbound=%s",
            [w.window_name for w in all_windows],
            bound_ids,
            [name for _, name, _ in unbound],
        )

        if unbound:
            # Show window picker
            logger.info(
                "Unbound topic: showing window picker (%d unbound windows, user=%d, thread=%d)",
                len(unbound),
                user.id,
                thread_id,
            )
            msg_text, keyboard, win_ids = build_window_picker(unbound)
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_SELECTING_WINDOW
                context.user_data[UNBOUND_WINDOWS_KEY] = win_ids
                context.user_data["_pending_thread_id"] = thread_id
                context.user_data["_pending_thread_text"] = text
            await safe_reply(update.message, msg_text, reply_markup=keyboard)
            return

        # No unbound windows — show directory browser to create a new session
        logger.info(
            "Unbound topic: showing directory browser (user=%d, thread=%d)",
            user.id,
            thread_id,
        )
        start_path = str(Path.cwd())
        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
            context.user_data["_pending_thread_id"] = thread_id
            context.user_data["_pending_thread_text"] = text
        await safe_reply(update.message, msg_text, reply_markup=keyboard)
        return

    # Bound topic — forward to bound window
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        logger.info(
            "Stale binding: window %s gone, unbinding (user=%d, thread=%d)",
            display,
            user.id,
            thread_id,
        )
        session_manager.unbind_thread(user.id, thread_id)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    await enqueue_status_update(context.bot, user.id, wid, None, thread_id=thread_id)

    # Cancel any running bash capture — new message pushes pane content down
    _cancel_bash_capture(user.id, thread_id)

    success, message = await session_manager.send_to_window(wid, text)
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    # Start background capture for ! bash command output
    if text.startswith("!") and len(text) > 1:
        bash_cmd = text[1:]  # strip leading "!"
        task = asyncio.create_task(
            _capture_bash_output(context.bot, user.id, thread_id, wid, bash_cmd)
        )
        _bash_capture_tasks[(user.id, thread_id)] = task

    # If in interactive mode, refresh the UI after sending text
    interactive_window = get_interactive_window(user.id, thread_id)
    if interactive_window and interactive_window == wid:
        await asyncio.sleep(0.2)
        await handle_interactive_ui(context.bot, user.id, wid, thread_id)


# --- Callback query handler ---


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        await query.answer("Not authorized")
        return

    data = query.data

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    cb_thread_id = _get_thread_id(update)
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, cb_thread_id, chat.id)

    # History: older/newer pagination
    # Format: hp:<page>:<window_id>:<start>:<end> or hn:<page>:<window_id>:<start>:<end>
    if data.startswith(CB_HISTORY_PREV) or data.startswith(CB_HISTORY_NEXT):
        prefix_len = len(CB_HISTORY_PREV)  # same length for both
        rest = data[prefix_len:]
        try:
            parts = rest.split(":")
            if len(parts) < 4:
                # Old format without byte range: page:window_id
                offset_str, window_id = rest.split(":", 1)
                start_byte, end_byte = 0, 0
            else:
                # New format: page:window_id:start:end (window_id may contain colons)
                offset_str = parts[0]
                start_byte = int(parts[-2])
                end_byte = int(parts[-1])
                window_id = ":".join(parts[1:-2])
            offset = int(offset_str)
        except (ValueError, IndexError):
            await query.answer("Invalid data")
            return

        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await send_history(
                query,
                window_id,
                offset=offset,
                edit=True,
                start_byte=start_byte,
                end_byte=end_byte,
                # Don't pass user_id for pagination - offset update only on initial view
                # This prevents offset from going backwards if new messages arrive while paging
            )
        else:
            await safe_edit(query, "Window no longer exists.")
        await query.answer("Page updated")

    # Directory browser handlers
    elif data.startswith(CB_DIR_SELECT):
        # Validate: callback must come from the same topic that started browsing
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        # callback_data contains index, not dir name (to avoid 64-byte limit)
        try:
            idx = int(data[len(CB_DIR_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        # Look up dir name from cached subdirs
        cached_dirs: list[str] = (
            context.user_data.get(BROWSE_DIRS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_dirs):
            await query.answer(
                "Directory list changed, please refresh", show_alert=True
            )
            return
        subdir_name = cached_dirs[idx]

        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        new_path = (Path(current_path) / subdir_name).resolve()

        if not new_path.exists() or not new_path.is_dir():
            await query.answer("Directory not found", show_alert=True)
            return

        new_path_str = str(new_path)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = new_path_str
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(new_path_str)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_UP:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        current = Path(current_path).resolve()
        parent = current.parent
        # No restriction - allow navigating anywhere

        parent_path = str(parent)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = parent_path
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(parent_path)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data.startswith(CB_DIR_PAGE):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        try:
            pg = int(data[len(CB_DIR_PAGE) :])
        except ValueError:
            await query.answer("Invalid data")
            return
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PAGE_KEY] = pg

        msg_text, keyboard, subdirs = build_directory_browser(current_path, pg)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_CONFIRM:
        default_path = str(Path.cwd())
        selected_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        # Check if this was initiated from a thread bind flow
        pending_thread_id: int | None = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )

        # Validate: confirm button must come from the same topic that started browsing
        confirm_thread_id = _get_thread_id(update)
        if pending_thread_id is not None and confirm_thread_id != pending_thread_id:
            clear_browse_state(context.user_data)
            if context.user_data is not None:
                context.user_data.pop("_pending_thread_id", None)
                context.user_data.pop("_pending_thread_text", None)
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return

        clear_browse_state(context.user_data)

        success, message, created_wname, created_wid = await tmux_manager.create_window(
            selected_path
        )
        if success:
            logger.info(
                "Window created: %s (id=%s) at %s (user=%d, thread=%s)",
                created_wname,
                created_wid,
                selected_path,
                user.id,
                pending_thread_id,
            )
            # Wait for session registration.
            # Claude: waits for SessionStart hook to write session_map.json
            # OpenCode: no hooks; the monitor discovers sessions by directory
            #   on the next poll cycle (~2s), so just bind immediately.
            if config.backend != "opencode":
                await session_manager.wait_for_session_map_entry(created_wid)

            if pending_thread_id is not None:
                # Thread bind flow: bind thread to newly created window
                session_manager.bind_thread(
                    user.id, pending_thread_id, created_wid, window_name=created_wname
                )

                # Rename the topic to match the window name
                resolved_chat = session_manager.resolve_chat_id(
                    user.id, pending_thread_id
                )
                try:
                    await context.bot.edit_forum_topic(
                        chat_id=resolved_chat,
                        message_thread_id=pending_thread_id,
                        name=created_wname,
                    )
                except Exception as e:
                    logger.debug(f"Failed to rename topic: {e}")

                await safe_edit(
                    query,
                    f"✅ {message}\n\nBound to this topic. Send messages here.",
                )

                # Send pending text if any
                pending_text = (
                    context.user_data.get("_pending_thread_text")
                    if context.user_data
                    else None
                )
                if pending_text:
                    logger.debug(
                        "Forwarding pending text to window %s (len=%d)",
                        created_wname,
                        len(pending_text),
                    )
                    if context.user_data is not None:
                        context.user_data.pop("_pending_thread_text", None)
                        context.user_data.pop("_pending_thread_id", None)
                    send_ok, send_msg = await session_manager.send_to_window(
                        created_wid,
                        pending_text,
                    )
                    if not send_ok:
                        logger.warning("Failed to forward pending text: %s", send_msg)
                        await safe_send(
                            context.bot,
                            resolved_chat,
                            f"❌ Failed to send pending message: {send_msg}",
                            message_thread_id=pending_thread_id,
                        )
                elif context.user_data is not None:
                    context.user_data.pop("_pending_thread_id", None)
            else:
                # Should not happen in topic-only mode, but handle gracefully
                await safe_edit(query, f"✅ {message}")
        else:
            await safe_edit(query, f"❌ {message}")
            if pending_thread_id is not None and context.user_data is not None:
                context.user_data.pop("_pending_thread_id", None)
                context.user_data.pop("_pending_thread_text", None)
        await query.answer("Created" if success else "Failed")

    elif data == CB_DIR_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        clear_browse_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Window picker: bind existing window
    elif data.startswith(CB_WIN_BIND):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        try:
            idx = int(data[len(CB_WIN_BIND) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_windows: list[str] = (
            context.user_data.get(UNBOUND_WINDOWS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_windows):
            await query.answer("Window list changed, please retry", show_alert=True)
            return
        selected_wid = cached_windows[idx]

        # Verify window still exists
        w = await tmux_manager.find_window_by_id(selected_wid)
        if not w:
            display = session_manager.get_display_name(selected_wid)
            await query.answer(f"Window '{display}' no longer exists", show_alert=True)
            return

        thread_id = _get_thread_id(update)
        if thread_id is None:
            await query.answer("Not in a topic", show_alert=True)
            return

        display = w.window_name
        clear_window_picker_state(context.user_data)
        session_manager.bind_thread(
            user.id, thread_id, selected_wid, window_name=display
        )

        # Rename the topic to match the window name
        resolved_chat = session_manager.resolve_chat_id(user.id, thread_id)
        try:
            await context.bot.edit_forum_topic(
                chat_id=resolved_chat,
                message_thread_id=thread_id,
                name=display,
            )
        except Exception as e:
            logger.debug(f"Failed to rename topic: {e}")

        await safe_edit(
            query,
            f"✅ Bound to window `{display}`",
        )

        # Forward pending text if any
        pending_text = (
            context.user_data.get("_pending_thread_text") if context.user_data else None
        )
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_text", None)
            context.user_data.pop("_pending_thread_id", None)
        if pending_text:
            send_ok, send_msg = await session_manager.send_to_window(
                selected_wid, pending_text
            )
            if not send_ok:
                logger.warning("Failed to forward pending text: %s", send_msg)
                await safe_send(
                    context.bot,
                    resolved_chat,
                    f"❌ Failed to send pending message: {send_msg}",
                    message_thread_id=thread_id,
                )
        await query.answer("Bound")

    # Window picker: new session → transition to directory browser
    elif data == CB_WIN_NEW:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        # Preserve pending thread info, clear only picker state
        clear_window_picker_state(context.user_data)
        start_path = str(Path.cwd())
        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    # Window picker: cancel
    elif data == CB_WIN_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        clear_window_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Screenshot: Refresh
    elif data.startswith(CB_SCREENSHOT_REFRESH):
        window_id = data[len(CB_SCREENSHOT_REFRESH) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window no longer exists", show_alert=True)
            return

        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if not text:
            await query.answer("Failed to capture pane", show_alert=True)
            return

        png_bytes = await text_to_image(text, with_ansi=True)
        keyboard = _build_screenshot_keyboard(window_id)
        try:
            await query.edit_message_media(
                media=InputMediaDocument(
                    media=io.BytesIO(png_bytes), filename="screenshot.png"
                ),
                reply_markup=keyboard,
            )
            await query.answer("Refreshed")
        except Exception as e:
            logger.error(f"Failed to refresh screenshot: {e}")
            await query.answer("Failed to refresh", show_alert=True)

    # --- Profile system callbacks ---
    elif data.startswith(CB_PROFILE_LAUNCH):
        slug = data[len(CB_PROFILE_LAUNCH):]
        profile = profile_manager.profiles.get(slug)
        if not profile:
            await query.answer("Profile not found", show_alert=True)
            return

        # Check if already active
        if profile_manager.is_active(slug):
            await query.answer("Already active!", show_alert=True)
            return

        # Check max active limit — suspend least recent if needed
        if profile_manager.active_count() >= profile_manager.MAX_ACTIVE:
            lru_slug = profile_manager.least_recent_active()
            if lru_slug:
                lru_state = profile_manager.get_state(lru_slug)
                lru_profile = profile_manager.profiles.get(lru_slug)
                if lru_state.window_id:
                    ws = session_manager.window_states.get(lru_state.window_id)
                    sid = ws.session_id if ws else ""
                    await tmux_manager.kill_window(lru_state.window_id)
                    profile_manager.suspend(lru_slug, session_id=sid)
                    lru_name = lru_profile.name if lru_profile else lru_slug
                    logger.info("Auto-suspended profile %s to make room", lru_name)

        # Build the CLI command
        cmd_parts = [
            profile.backend if profile.backend != "claude" else config.claude_command
        ]
        if profile.model:
            cmd_parts.append(f"--model {profile.model}")
        if profile.flags:
            cmd_parts.append(profile.flags)

        # Check if we should resume
        pstate = profile_manager.get_state(slug)
        if profile.resume and pstate.last_session_id:
            cmd_parts.append(f"--resume {pstate.last_session_id}")

        cli_command = " ".join(cmd_parts)

        # Create tmux window
        success, msg, wname, wid = await tmux_manager.create_window(
            work_dir=profile.directory,
            window_name=profile.slug,
            start_claude=False,
        )

        if not success:
            await safe_edit(query, f"❌ Failed to launch: {msg}")
            await query.answer()
            return

        # Send the CLI command to the new window
        await tmux_manager.send_keys(wid, cli_command, enter=True, literal=True)

        # Track in profile state
        thread_id = _get_thread_id(update)
        chat = update.effective_chat
        chat_id = chat.id if chat else 0
        profile_manager.activate(
            slug,
            window_id=wid,
            topic_id=thread_id or 0,
            chat_id=chat_id,
        )

        # Bind the topic to this window if we have a thread
        if thread_id and user:
            session_manager.bind_thread(user.id, thread_id, wid, window_name=profile.slug)
            session_manager.window_display_names[wid] = profile.name
            session_manager._save_state()

        # Inject system prompt after a delay (let CLI start up)
        if profile.system_prompt:
            async def _inject_prompt() -> None:
                await asyncio.sleep(8)
                await tmux_manager.send_keys(wid, profile.system_prompt, enter=True, literal=True)
            asyncio.create_task(_inject_prompt())

        deep_link = profile_manager.get_telegram_deep_link(slug)
        link_text = f"\n🔗 [Open in Telegram]({deep_link})" if deep_link else ""

        await safe_edit(
            query,
            f"{profile.icon} *{profile.name}* launched\n"
            f"📂 `{profile.directory}`\n"
            f"⚙️ {profile.backend}"
            f"{f' ({profile.model})' if profile.model else ''}"
            f"{link_text}",
        )
        await query.answer("Launched!")

    elif data.startswith(CB_PROFILE_SUSPEND):
        slug = data[len(CB_PROFILE_SUSPEND):]
        pstate = profile_manager.get_state(slug)
        profile = profile_manager.profiles.get(slug)
        if not pstate.window_id:
            await query.answer("Not active", show_alert=True)
            return

        ws = session_manager.window_states.get(pstate.window_id)
        sid = ws.session_id if ws else ""

        await tmux_manager.kill_window(pstate.window_id)
        profile_manager.suspend(slug, session_id=sid)

        name = profile.name if profile else slug
        await safe_edit(query, f"💤 *{name}* suspended. Session saved for resume.")
        await query.answer("Suspended")

    elif data.startswith(CB_PROFILE_INFO):
        slug = data[len(CB_PROFILE_INFO):]
        profile = profile_manager.profiles.get(slug)
        pstate = profile_manager.get_state(slug)
        if not profile:
            await query.answer("Profile not found", show_alert=True)
            return

        is_active = bool(pstate.window_id)
        status = "● Active" if is_active else "○ Suspended"

        deep_link = profile_manager.get_telegram_deep_link(slug)
        link_text = f"\n🔗 [Open topic]({deep_link})" if deep_link else ""

        info_text = (
            f"{profile.icon} *{profile.name}*\n"
            f"Status: {status}\n"
            f"📂 `{profile.directory}`\n"
            f"⚙️ {profile.backend}"
            f"{f' ({profile.model})' if profile.model else ''}"
            f"{link_text}"
        )

        buttons = []
        if is_active:
            buttons.append([
                InlineKeyboardButton("💤 Suspend", callback_data=f"pf:suspend:{slug}"),
            ])
        else:
            buttons.append([
                InlineKeyboardButton("🚀 Launch", callback_data=f"pf:launch:{slug}"),
            ])

        keyboard = InlineKeyboardMarkup(buttons) if buttons else None
        await safe_edit(query, info_text, reply_markup=keyboard)
        await query.answer()

    # --- Launch builder callbacks ---
    elif data.startswith("lb:"):
        thread_id = _get_thread_id(update)
        key = _lb_key(thread_id)
        lb = (context.user_data or {}).get(key)

        # Quick-launch a saved profile from the builder menu
        if data.startswith(CB_LB_PROFILE):
            slug = data[len(CB_LB_PROFILE):]
            profile = profile_manager.profiles.get(slug)
            if not profile:
                await query.answer("Profile not found", show_alert=True)
                return

            # Reuse the profile launch logic: build CLI cmd + create window
            pstate = profile_manager.get_state(slug)

            # If already active, just tell the user
            if profile_manager.is_active(slug):
                await safe_edit(query, f"{profile.icon} *{profile.name}* is already active.")
                await query.answer()
                return

            # Auto-suspend LRU if at capacity
            if profile_manager.active_count() >= profile_manager.MAX_ACTIVE:
                lru_slug = profile_manager.least_recent_active()
                if lru_slug:
                    lru_state = profile_manager.get_state(lru_slug)
                    if lru_state.window_id:
                        ws = session_manager.window_states.get(lru_state.window_id)
                        sid = ws.session_id if ws else ""
                        await tmux_manager.kill_window(lru_state.window_id)
                        profile_manager.suspend(lru_slug, session_id=sid)

            cmd_parts = [
                profile.backend if profile.backend != "claude" else config.claude_command
            ]
            if profile.model:
                cmd_parts.append(f"--model {profile.model}")
            if profile.flags:
                cmd_parts.append(profile.flags)
            if profile.resume and pstate.last_session_id:
                cmd_parts.append(f"--resume {pstate.last_session_id}")
            cli_command = " ".join(cmd_parts)

            success, msg, wname, wid = await tmux_manager.create_window(
                work_dir=profile.directory,
                window_name=profile.slug,
                start_claude=False,
            )
            if not success:
                await safe_edit(query, f"❌ Failed: {msg}")
                await query.answer()
                return

            await tmux_manager.send_keys(wid, cli_command, enter=True, literal=True)

            chat = update.effective_chat
            chat_id = chat.id if chat else 0
            profile_manager.activate(slug, window_id=wid, topic_id=thread_id or 0, chat_id=chat_id)
            if thread_id and user:
                session_manager.bind_thread(user.id, thread_id, wid, window_name=profile.slug)
                session_manager.window_display_names[wid] = profile.name
                session_manager._save_state()

            if profile.system_prompt:
                async def _inject(w=wid, p=profile.system_prompt) -> None:
                    await asyncio.sleep(8)
                    await tmux_manager.send_keys(w, p, enter=True, literal=True)
                asyncio.create_task(_inject())

            await safe_edit(
                query,
                f"{profile.icon} *{profile.name}* launched\n"
                f"📂 `{_short_dir(profile.directory)}`\n"
                f"⚙️ {profile.backend}"
                f"{f' ({profile.model})' if profile.model else ''}",
            )
            await query.answer("Launched!")
            return

        if not lb:
            # No builder state — start fresh
            lb = {
                "step": "main", "backend": "", "model": "", "dir_idx": -1,
                "known_dirs": _get_known_dirs(), "skip_perms": True, "resume": True,
            }
            if context.user_data is not None:
                context.user_data[key] = lb

        # Step: pick backend
        if data.startswith(CB_LB_BACKEND):
            val = data[len(CB_LB_BACKEND):]
            if val == "pick":
                lb["step"] = "backend"
            else:
                lb["backend"] = val
                lb["step"] = "model"

        # Step: pick model
        elif data.startswith(CB_LB_MODEL):
            val = data[len(CB_LB_MODEL):]
            lb["model"] = "" if val == "default" else val
            lb["step"] = "dir"

        # Step: pick directory
        elif data.startswith(CB_LB_DIR):
            val = data[len(CB_LB_DIR):]
            if val == "browse":
                # TODO: wire into directory browser
                await query.answer("Browse not yet wired — pick a known dir", show_alert=True)
                return
            lb["dir_idx"] = int(val)
            lb["step"] = "flags"

        # Step: toggle flags
        elif data.startswith(CB_LB_FLAG):
            flag = data[len(CB_LB_FLAG):]
            if flag == "skip":
                lb["skip_perms"] = not lb.get("skip_perms", True)
            elif flag == "resume":
                lb["resume"] = not lb.get("resume", True)
            # Stay on flags step

        # Launch!
        elif data == CB_LB_GO:
            known_dirs = lb.get("known_dirs", [])
            dir_idx = lb.get("dir_idx", -1)
            if dir_idx < 0 or dir_idx >= len(known_dirs):
                await query.answer("Pick a directory first", show_alert=True)
                return

            backend = lb.get("backend", "claude")
            model = lb.get("model", "")
            work_dir = known_dirs[dir_idx]
            skip_perms = lb.get("skip_perms", True)
            do_resume = lb.get("resume", True)

            cmd_parts = [
                backend if backend != "claude" else config.claude_command
            ]
            if model:
                cmd_parts.append(f"--model {model}")
            if skip_perms:
                cmd_parts.append("--dangerously-skip-permissions")

            cli_command = " ".join(cmd_parts)
            dir_name = Path(work_dir).name

            success, msg, wname, wid = await tmux_manager.create_window(
                work_dir=work_dir,
                window_name=dir_name,
                start_claude=False,
            )
            if not success:
                await safe_edit(query, f"❌ Launch failed: {msg}")
                await query.answer()
                return

            await tmux_manager.send_keys(wid, cli_command, enter=True, literal=True)

            if thread_id and user:
                session_manager.bind_thread(user.id, thread_id, wid, window_name=dir_name)
                session_manager.window_display_names[wid] = dir_name
                session_manager._save_state()

            model_note = f" ({model})" if model else ""
            await safe_edit(
                query,
                f"🚀 *Launched!*\n"
                f"📂 `{_short_dir(work_dir)}`\n"
                f"⚙️ {backend}{model_note}",
            )
            await query.answer("Launched!")

            # Clean up builder state
            if context.user_data is not None:
                context.user_data.pop(key, None)
            return

        # Re-render the builder
        text, keyboard = _build_lb_message(lb)
        await safe_edit(query, text, reply_markup=keyboard)
        await query.answer()

    elif data == "noop":
        await query.answer()

    # Interactive UI: Up arrow
    elif data.startswith(CB_ASK_UP):
        window_id = data[len(CB_ASK_UP) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(w.window_id, "Up", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer()

    # Interactive UI: Down arrow
    elif data.startswith(CB_ASK_DOWN):
        window_id = data[len(CB_ASK_DOWN) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Down", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer()

    # Interactive UI: Left arrow
    elif data.startswith(CB_ASK_LEFT):
        window_id = data[len(CB_ASK_LEFT) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Left", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer()

    # Interactive UI: Right arrow
    elif data.startswith(CB_ASK_RIGHT):
        window_id = data[len(CB_ASK_RIGHT) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Right", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer()

    # Interactive UI: Escape
    elif data.startswith(CB_ASK_ESC):
        window_id = data[len(CB_ASK_ESC) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Escape", enter=False, literal=False
            )
            await clear_interactive_msg(user.id, context.bot, thread_id)
        await query.answer("⎋ Esc")

    # Interactive UI: Enter
    elif data.startswith(CB_ASK_ENTER):
        window_id = data[len(CB_ASK_ENTER) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Enter", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer("⏎ Enter")

    # Interactive UI: Space
    elif data.startswith(CB_ASK_SPACE):
        window_id = data[len(CB_ASK_SPACE) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Space", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer("␣ Space")

    # Interactive UI: Tab
    elif data.startswith(CB_ASK_TAB):
        window_id = data[len(CB_ASK_TAB) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(w.window_id, "Tab", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer("⇥ Tab")

    # Interactive UI: refresh display
    elif data.startswith(CB_ASK_REFRESH):
        window_id = data[len(CB_ASK_REFRESH) :]
        thread_id = _get_thread_id(update)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer("🔄")

    # Screenshot quick keys: send key to tmux window
    elif data.startswith(CB_KEYS_PREFIX):
        rest = data[len(CB_KEYS_PREFIX) :]
        colon_idx = rest.find(":")
        if colon_idx < 0:
            await query.answer("Invalid data")
            return
        key_id = rest[:colon_idx]
        window_id = rest[colon_idx + 1 :]

        key_info = _KEYS_SEND_MAP.get(key_id)
        if not key_info:
            await query.answer("Unknown key")
            return

        tmux_key, enter, literal = key_info
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return

        await tmux_manager.send_keys(
            w.window_id, tmux_key, enter=enter, literal=literal
        )
        await query.answer(_KEY_LABELS.get(key_id, key_id))

        # Refresh screenshot after key press
        await asyncio.sleep(0.5)
        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if text:
            png_bytes = await text_to_image(text, with_ansi=True)
            keyboard = _build_screenshot_keyboard(window_id)
            try:
                await query.edit_message_media(
                    media=InputMediaDocument(
                        media=io.BytesIO(png_bytes),
                        filename="screenshot.png",
                    ),
                    reply_markup=keyboard,
                )
            except Exception:
                pass  # Screenshot unchanged or message too old


# --- Streaming response / notifications ---


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Handle a new assistant message — enqueue for sequential processing.

    Messages are queued per-user to ensure status messages always appear last.
    Routes via thread_bindings to deliver to the correct topic.
    """
    status = "complete" if msg.is_complete else "streaming"
    logger.info(
        f"handle_new_message [{status}]: session={msg.session_id}, "
        f"text_len={len(msg.text)}"
    )

    # Find users whose thread-bound window matches this session
    if config.backend == "opencode":
        active_users = session_manager.find_users_for_session_direct(msg.session_id)
    else:
        active_users = await session_manager.find_users_for_session(msg.session_id)

    if not active_users:
        logger.info(f"No active users for session {msg.session_id}")
        return

    for user_id, wid, thread_id in active_users:
        # Handle interactive tools specially - capture terminal and send UI
        if msg.tool_name in INTERACTIVE_TOOL_NAMES and msg.content_type == "tool_use":
            # Mark interactive mode BEFORE sleeping so polling skips this window
            set_interactive_mode(user_id, wid, thread_id)
            # Flush pending messages (e.g. plan content) before sending interactive UI
            queue = get_message_queue(user_id)
            if queue:
                await queue.join()
            # Wait briefly for Claude Code to render the question UI
            await asyncio.sleep(0.3)
            handled = await handle_interactive_ui(bot, user_id, wid, thread_id)
            if handled:
                # Update user's read offset (Claude Code backend only)
                if config.backend != "opencode":
                    session = await session_manager.resolve_session_for_window(wid)
                    if session and session.file_path:
                        try:
                            file_size = Path(session.file_path).stat().st_size
                            session_manager.update_user_window_offset(
                                user_id, wid, file_size
                            )
                        except OSError:
                            pass
                continue  # Don't send the normal tool_use message
            else:
                # UI not rendered — clear the early-set mode
                clear_interactive_mode(user_id, thread_id)

        # Any non-interactive message means the interaction is complete — delete the UI message
        if get_interactive_msg_id(user_id, thread_id):
            await clear_interactive_msg(user_id, bot, thread_id)

        # Content filter: decide what's worth showing on Telegram
        from .content_filter import VerboseLevel, filter_message

        decision = filter_message(
            msg.text,
            msg.content_type,
            msg.tool_name,
            msg.role,
            VerboseLevel(config.verbose_level),
        )
        if decision.action == "suppress":
            logger.debug(
                "Filtered [%s/%s]: suppressed (verbose=%d)",
                msg.content_type,
                msg.tool_name or "",
                config.verbose_level,
            )
            continue

        display_text = decision.text or msg.text

        parts = build_response_parts(
            display_text,
            msg.is_complete,
            msg.content_type,
            msg.role,
        )

        if msg.is_complete:
            # Enqueue content message task
            # Note: tool_result editing is handled inside _process_content_task
            # to ensure sequential processing with tool_use message sending
            await enqueue_content_message(
                bot=bot,
                user_id=user_id,
                window_id=wid,
                parts=parts,
                tool_use_id=msg.tool_use_id,
                content_type=msg.content_type,
                text=msg.text,
                thread_id=thread_id,
                image_data=msg.image_data,
            )

            # Update user's read offset to current file position
            # This marks these messages as "read" for this user
            # (Claude Code backend only — opencode uses time-based cursors)
            if config.backend != "opencode":
                session = await session_manager.resolve_session_for_window(wid)
                if session and session.file_path:
                    try:
                        file_size = Path(session.file_path).stat().st_size
                        session_manager.update_user_window_offset(
                            user_id, wid, file_size
                        )
                    except OSError:
                        pass


# --- App lifecycle ---


async def post_init(application: Application) -> None:
    global session_monitor, _status_poll_task

    await application.bot.delete_my_commands()

    bot_commands = [
        BotCommand("help", "Command reference — what do I need?"),
        BotCommand("start", "Show welcome message"),
        BotCommand("history", "Message history for this topic"),
        BotCommand("screenshot", "Terminal screenshot with control keys"),
        BotCommand("esc", "Send Escape to interrupt Claude"),
        BotCommand("kill", "Kill session and delete topic"),
        BotCommand("unbind", "Unbind topic from session (keeps window running)"),
        BotCommand("verbose", "Set verbosity: /verbose 0|1|2"),
        BotCommand("flush", "Clear message backlog for this topic"),
        BotCommand("usage", "Show Claude Code usage remaining"),
        BotCommand("launch", "Interactive session launcher"),
        BotCommand("profiles", "Launch/manage workspace profiles"),
        BotCommand("status", "Health check of entire stack"),
        BotCommand("ps", "Show running processes in all windows"),
        BotCommand("diag", "Full diagnostic dump"),
        BotCommand("alive", "Check if Claude/OpenCode is still writing"),
        BotCommand("relaunch", "Exit + reopen Claude (reload MCP configs etc)"),
        BotCommand("cmds", "Searchable command list: /cmds [search]"),
        BotCommand("sessionconfig", "Show session/profile config for this topic"),
    ]
    # Add Claude Code slash commands
    for cmd_name, desc in CC_COMMANDS.items():
        bot_commands.append(BotCommand(cmd_name, desc))

    await application.bot.set_my_commands(bot_commands)

    # Re-resolve stale window IDs from persisted state against live tmux windows
    await session_manager.resolve_stale_ids()

    # Pre-fill global rate limiter bucket on restart.
    # AsyncLimiter starts at _level=0 (full burst capacity), but Telegram's
    # server-side counter persists across bot restarts.  Setting _level=max_rate
    # forces the bucket to start "full" so capacity drains in naturally (~1s).
    # AIORateLimiter has no per-private-chat limiter, so max_retries is the
    # primary protection (retry + pause all concurrent requests on 429).
    rate_limiter = application.bot.rate_limiter
    if rate_limiter and rate_limiter._base_limiter:
        rate_limiter._base_limiter._level = rate_limiter._base_limiter.max_rate
        logger.info("Pre-filled global rate limiter bucket")

    if config.backend == "opencode":
        monitor = OpenCodeMonitor()
        logger.info("Using OpenCode backend (SQLite: %s)", config.opencode_db_path)
    else:
        monitor = SessionMonitor()
        logger.info("Using Claude Code backend (projects: %s)", config.claude_projects_path)

    async def message_callback(msg: NewMessage) -> None:
        await handle_new_message(msg, application.bot)

    monitor.set_message_callback(message_callback)
    monitor.start()
    session_monitor = monitor
    logger.info("Session monitor started")

    # Start status polling task
    _status_poll_task = asyncio.create_task(status_poll_loop(application.bot))
    logger.info("Status polling task started")


async def post_shutdown(application: Application) -> None:
    global _status_poll_task

    # Stop status polling
    if _status_poll_task:
        _status_poll_task.cancel()
        try:
            await _status_poll_task
        except asyncio.CancelledError:
            pass
        _status_poll_task = None
        logger.info("Status polling stopped")

    # Stop all queue workers
    await shutdown_workers()

    if session_monitor:
        session_monitor.stop()
        logger.info("Session monitor stopped")


def create_bot() -> Application:
    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cmds", cmds_command))
    application.add_handler(CommandHandler("sessionconfig", sessionconfig_command))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("esc", esc_command))
    application.add_handler(CommandHandler("unbind", unbind_command))
    application.add_handler(CommandHandler("verbose", verbose_command))
    application.add_handler(CommandHandler("flush", flush_command))
    application.add_handler(CommandHandler("usage", usage_command))
    application.add_handler(CommandHandler("profiles", profiles_command))
    application.add_handler(CommandHandler("launch", launch_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("ps", ps_command))
    application.add_handler(CommandHandler("diag", diag_command))
    application.add_handler(CommandHandler("alive", alive_command))
    application.add_handler(CommandHandler("relaunch", relaunch_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    # Topic closed event — auto-kill associated window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CLOSED,
            topic_closed_handler,
        )
    )
    # Forward any other /command to Claude Code
    application.add_handler(MessageHandler(filters.COMMAND, forward_command_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    # Photos: download and forward file path to Claude Code
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    # Catch-all: non-text content (stickers, voice, etc.)
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL,
            unsupported_content_handler,
        )
    )

    return application
