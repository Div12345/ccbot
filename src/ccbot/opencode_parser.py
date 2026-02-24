"""Parser for OpenCode SQLite session data.

Maps OpenCode part types to ccbot's ParsedEntry format for Telegram display.
Reuses ParsedEntry from transcript_parser.py so the Telegram rendering layer
needs zero changes.

Part type mapping:
  opencode "text"      -> ParsedEntry content_type="text"
  opencode "tool"      -> ParsedEntry content_type="tool_use" or "tool_result"
  opencode "reasoning"  -> ParsedEntry content_type="thinking"
  opencode "patch"     -> ParsedEntry content_type="text" (file change summary)
  opencode "step-start"/"step-finish"/"compaction" -> skipped

Key function: parse_parts().
"""

import logging
from typing import Any

from .transcript_parser import ParsedEntry, TranscriptParser

logger = logging.getLogger(__name__)

# Map opencode tool names (lowercase) to display names matching Claude Code style
TOOL_NAME_MAP: dict[str, str] = {
    "bash": "Bash",
    "interactive_bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "task": "Task",
    "delegate_task": "Task",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "question": "AskUserQuestion",
    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
    "apply_patch": "ApplyPatch",
    "lsp_diagnostics": "LSP",
    "look_at": "Read",
    "session_search": "Search",
    "session_read": "Read",
    "session_list": "List",
    "session_info": "Info",
    "background_output": "Background",
    "background_cancel": "Background",
    "slashcommand": "Command",
    "skill": "Skill",
    "skill_mcp": "Skill",
}


def _normalize_tool_name(name: str) -> str:
    """Map opencode tool name to display name."""
    if name in TOOL_NAME_MAP:
        return TOOL_NAME_MAP[name]
    # MCP tools like "obsidian_obsidian_read_note" or "paper-search_search_arxiv"
    # Display as "obsidian:read_note" or "paper-search:search_arxiv"
    if "_" in name:
        # Split on first underscore for MCP prefix
        prefix, _, rest = name.partition("_")
        # Some MCP tools double the prefix: "obsidian_obsidian_read_note"
        if rest.startswith(prefix + "_"):
            rest = rest[len(prefix) + 1 :]
        return f"{prefix}:{rest}" if rest else name
    return name.capitalize()


def _camel_to_snake(s: str) -> str:
    """Convert camelCase to snake_case."""
    result: list[str] = []
    for c in s:
        if c.isupper():
            result.append("_")
            result.append(c.lower())
        else:
            result.append(c)
    return "".join(result).lstrip("_")


def _normalize_input(input_data: dict[str, Any]) -> dict[str, Any]:
    """Normalize opencode's camelCase input keys to snake_case.

    OpenCode uses camelCase (filePath, oldString) while ccbot's
    format_tool_use_summary expects snake_case (file_path, old_string).
    """
    if not isinstance(input_data, dict):
        return {}
    return {_camel_to_snake(k): v for k, v in input_data.items()}


def format_tool_summary(tool_name: str, input_data: dict[str, Any] | Any) -> str:
    """Format tool invocation summary for Telegram display."""
    display_name = _normalize_tool_name(tool_name)
    if not isinstance(input_data, dict):
        return f"**{display_name}**"
    normalized = _normalize_input(input_data)
    return TranscriptParser.format_tool_use_summary(display_name, normalized)


def parse_parts(
    parts: list[dict[str, Any]],
    message_roles: dict[str, str],
) -> list[ParsedEntry]:
    """Parse OpenCode parts into ParsedEntry objects.

    Args:
        parts: List of part data dicts from SQLite (with injected
               ``_message_id``, ``_timestamp`` metadata).
        message_roles: Map of message_id -> role ("user" | "assistant").

    Returns:
        List of ParsedEntry objects ready for Telegram display.
    """
    result: list[ParsedEntry] = []

    for part in parts:
        ptype = part.get("type", "")
        msg_id = part.get("_message_id", "")
        timestamp = part.get("_timestamp")
        role = message_roles.get(msg_id, "assistant")

        if ptype == "text":
            text = part.get("text", "").strip()
            if not text:
                continue
            result.append(
                ParsedEntry(
                    role=role,
                    text=text,
                    content_type="text",
                    timestamp=timestamp,
                )
            )

        elif ptype == "reasoning":
            text = part.get("text", "").strip()
            if not text:
                continue
            quoted = TranscriptParser._format_expandable_quote(text)
            result.append(
                ParsedEntry(
                    role="assistant",
                    text=quoted,
                    content_type="thinking",
                    timestamp=timestamp,
                )
            )

        elif ptype == "tool":
            tool_name = part.get("tool", "unknown")
            call_id = part.get("callID", "")
            state = part.get("state", {})
            status = state.get("status", "")
            input_data = state.get("input", {})
            output = state.get("output", "")

            display_name = _normalize_tool_name(tool_name)
            summary = format_tool_summary(tool_name, input_data)

            if status == "running":
                # Tool invoked but not yet complete
                result.append(
                    ParsedEntry(
                        role="assistant",
                        text=summary,
                        content_type="tool_use",
                        tool_use_id=call_id or None,
                        timestamp=timestamp,
                        tool_name=display_name,
                    )
                )
            elif status == "completed":
                entry_text = summary
                if output:
                    formatted = TranscriptParser._format_tool_result_text(
                        output, display_name
                    )
                    if formatted:
                        entry_text += "\n" + formatted
                result.append(
                    ParsedEntry(
                        role="assistant",
                        text=entry_text,
                        content_type="tool_result",
                        tool_use_id=call_id or None,
                        timestamp=timestamp,
                        tool_name=display_name,
                    )
                )
            elif status == "error":
                entry_text = summary
                error_msg = output or "Unknown error"
                error_summary = error_msg.split("\n")[0]
                if len(error_summary) > 100:
                    error_summary = error_summary[:100] + "\u2026"
                entry_text += f"\n  \u23bf  Error: {error_summary}"
                if "\n" in error_msg:
                    entry_text += (
                        "\n"
                        + TranscriptParser._format_expandable_quote(error_msg)
                    )
                result.append(
                    ParsedEntry(
                        role="assistant",
                        text=entry_text,
                        content_type="tool_result",
                        tool_use_id=call_id or None,
                        timestamp=timestamp,
                    )
                )

        elif ptype == "patch":
            files = part.get("files", [])
            if files:
                file_names = [f.rsplit("/", 1)[-1] for f in files[:5]]
                file_list = ", ".join(file_names)
                if len(files) > 5:
                    file_list += f" (+{len(files) - 5} more)"
                result.append(
                    ParsedEntry(
                        role="assistant",
                        text=f"Files changed: {file_list}",
                        content_type="text",
                        timestamp=timestamp,
                    )
                )

        # Skip: step-start, step-finish, compaction, file

    return result
