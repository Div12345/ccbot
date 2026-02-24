"""Content filter for controlling message verbosity on Telegram.

Sits between session_monitor/handle_new_message and the message queue.
Decides what's worth sending to a phone screen vs what's noise.

Levels:
  0 (QUIET):   Only final text responses. Zero tool noise.
  1 (NORMAL):  Tool names + emoji icon, suppress tool_result + thinking.
  2 (VERBOSE): Everything (current behavior, backward compatible).
"""

import logging
from dataclasses import dataclass
from enum import IntEnum

logger = logging.getLogger(__name__)

TOOL_ICONS: dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Grep": "\U0001f50d",
    "Glob": "\U0001f4c1",
    "Task": "\U0001f916",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "TodoWrite": "\U0001f4dd",
    "NotebookEdit": "\U0001f4d3",
}

# Tools that require user interaction — always show
INTERACTIVE_TOOLS = {"AskUserQuestion"}


class VerboseLevel(IntEnum):
    QUIET = 0
    NORMAL = 1
    VERBOSE = 2


@dataclass
class FilterResult:
    """Result of filtering a message."""

    action: str  # "send", "suppress"
    text: str = ""  # Possibly rewritten text


def filter_message(
    text: str,
    content_type: str,
    tool_name: str | None,
    role: str,
    verbose_level: VerboseLevel,
) -> FilterResult:
    """Decide whether/how to show a message based on verbose level.

    Returns a FilterResult with action="send" or "suppress".
    """
    # Level 2: pass everything through (current behavior)
    if verbose_level >= VerboseLevel.VERBOSE:
        return FilterResult(action="send", text=text)

    # Always show final assistant text responses
    if content_type == "text" and role == "assistant":
        return FilterResult(action="send", text=text)

    # Always show user messages (filtered separately by show_user_messages)
    if role == "user":
        return FilterResult(action="send", text=text)

    # Always show interactive tool prompts
    if tool_name in INTERACTIVE_TOOLS:
        return FilterResult(action="send", text=text)

    # --- Level 0 (QUIET): suppress all tool activity ---
    if verbose_level == VerboseLevel.QUIET:
        if content_type in ("tool_use", "tool_result", "thinking"):
            return FilterResult(action="suppress")

    # --- Level 1 (NORMAL): brief tool_use, suppress tool_result + thinking ---
    if verbose_level == VerboseLevel.NORMAL:
        if content_type == "tool_result":
            return FilterResult(action="suppress")

        if content_type == "thinking":
            return FilterResult(action="suppress")

        if content_type == "tool_use":
            icon = TOOL_ICONS.get(tool_name or "", "\U0001f527")
            # Extract a brief summary from the text
            brief = _brief_tool_summary(text, tool_name, max_len=80)
            return FilterResult(action="send", text=f"{icon} {brief}")

    # Default: send as-is
    return FilterResult(action="send", text=text)


def _brief_tool_summary(text: str, tool_name: str | None, max_len: int = 80) -> str:
    """Create a brief one-liner from tool_use text."""
    if not text:
        return tool_name or "Working..."

    # Take first line, truncate
    first_line = text.split("\n")[0].strip()
    if len(first_line) > max_len:
        return first_line[:max_len] + "..."
    return first_line
