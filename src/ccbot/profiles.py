"""Profile system for reusable workspace definitions.

Profiles define named workspaces (directory + backend + model + flags + system prompt)
that can be launched/suspended/resumed from Telegram via /profiles command.

Storage: ~/.ccbot/profiles/*.json
State: ~/.ccbot/profile_state.json (tracks active profiles, topic IDs, session IDs)
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import config
from .utils import atomic_write_json

logger = logging.getLogger(__name__)


@dataclass
class Profile:
    """A reusable workspace definition."""

    slug: str  # URL-safe identifier, also filename stem
    name: str  # Display name (e.g. "Arterial Analysis")
    icon: str = "📂"  # Emoji icon for inline keyboard
    directory: str = ""  # Working directory path
    backend: str = "claude"  # "claude" or "opencode"
    model: str = ""  # Optional model override (e.g. "opus", "sonnet")
    flags: str = "--dangerously-skip-permissions"  # CLI flags
    system_prompt: str = ""  # Injected as first message after launch
    resume: bool = True  # Try to resume last session
    obsidian_note: str = ""  # Vault-relative path for bidirectional linking
    max_idle_minutes: int = 60  # Auto-suspend after this much idle time

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "icon": self.icon,
            "directory": self.directory,
            "backend": self.backend,
            "model": self.model,
            "flags": self.flags,
            "system_prompt": self.system_prompt,
            "resume": self.resume,
            "obsidian_note": self.obsidian_note,
            "max_idle_minutes": self.max_idle_minutes,
        }

    @classmethod
    def from_dict(cls, slug: str, data: dict[str, Any]) -> "Profile":
        return cls(
            slug=slug,
            name=data.get("name", slug),
            icon=data.get("icon", "📂"),
            directory=data.get("directory", ""),
            backend=data.get("backend", "claude"),
            model=data.get("model", ""),
            flags=data.get("flags", "--dangerously-skip-permissions"),
            system_prompt=data.get("system_prompt", ""),
            resume=data.get("resume", True),
            obsidian_note=data.get("obsidian_note", ""),
            max_idle_minutes=data.get("max_idle_minutes", 60),
        )


@dataclass
class ProfileState:
    """Runtime state for a profile (persisted separately from profile definition)."""

    slug: str
    window_id: str = ""  # Current tmux window ID (empty = suspended)
    topic_id: int = 0  # Telegram topic/thread ID (0 = no topic yet)
    last_session_id: str = ""  # Claude/OpenCode session ID for resume
    last_active: float = 0.0  # Unix timestamp of last activity
    chat_id: int = 0  # Telegram chat_id (for deep links)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "topic_id": self.topic_id,
            "last_session_id": self.last_session_id,
            "last_active": self.last_active,
            "chat_id": self.chat_id,
        }

    @classmethod
    def from_dict(cls, slug: str, data: dict[str, Any]) -> "ProfileState":
        return cls(
            slug=slug,
            window_id=data.get("window_id", ""),
            topic_id=data.get("topic_id", 0),
            last_session_id=data.get("last_session_id", ""),
            last_active=data.get("last_active", 0.0),
            chat_id=data.get("chat_id", 0),
        )


class ProfileManager:
    """Manages profile definitions and runtime state."""

    MAX_ACTIVE = 4  # Max concurrent tmux windows from profiles

    def __init__(self) -> None:
        self.profiles_dir = config.config_dir / "profiles"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = config.config_dir / "profile_state.json"

        self.profiles: dict[str, Profile] = {}
        self.states: dict[str, ProfileState] = {}

        self._load_profiles()
        self._load_states()

    def _load_profiles(self) -> None:
        """Load all profile JSON files from profiles_dir."""
        self.profiles.clear()
        for f in sorted(self.profiles_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                slug = f.stem
                self.profiles[slug] = Profile.from_dict(slug, data)
                logger.debug("Loaded profile: %s", slug)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Failed to load profile %s: %s", f.name, e)

    def _load_states(self) -> None:
        """Load profile runtime states."""
        self.states.clear()
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                for slug, state_data in data.items():
                    self.states[slug] = ProfileState.from_dict(slug, state_data)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Failed to load profile states: %s", e)

    def _save_states(self) -> None:
        """Persist profile runtime states."""
        data = {slug: state.to_dict() for slug, state in self.states.items()}
        atomic_write_json(self.state_file, data)

    def get_state(self, slug: str) -> ProfileState:
        """Get or create state for a profile."""
        if slug not in self.states:
            self.states[slug] = ProfileState(slug=slug)
        return self.states[slug]

    def list_profiles(self) -> list[tuple[Profile, ProfileState]]:
        """Return all profiles with their states, sorted by name."""
        result = []
        for slug, profile in sorted(self.profiles.items(), key=lambda x: x[1].name):
            state = self.get_state(slug)
            result.append((profile, state))
        return result

    def is_active(self, slug: str) -> bool:
        """Check if a profile has a live tmux window."""
        state = self.states.get(slug)
        return bool(state and state.window_id)

    def active_count(self) -> int:
        """Count currently active profiles."""
        return sum(1 for s in self.states.values() if s.window_id)

    def least_recent_active(self) -> str | None:
        """Find the least recently active profile slug (for suspension)."""
        active = [
            (slug, state)
            for slug, state in self.states.items()
            if state.window_id
        ]
        if not active:
            return None
        active.sort(key=lambda x: x[1].last_active)
        return active[0][0]

    def save_profile(self, profile: Profile) -> None:
        """Save a profile definition to disk."""
        self.profiles[profile.slug] = profile
        path = self.profiles_dir / f"{profile.slug}.json"
        path.write_text(json.dumps(profile.to_dict(), indent=2))
        logger.info("Saved profile: %s", profile.slug)

    def activate(
        self,
        slug: str,
        window_id: str,
        topic_id: int = 0,
        chat_id: int = 0,
    ) -> None:
        """Mark a profile as active with a tmux window."""
        state = self.get_state(slug)
        state.window_id = window_id
        if topic_id:
            state.topic_id = topic_id
        if chat_id:
            state.chat_id = chat_id
        state.last_active = time.time()
        self._save_states()

    def suspend(self, slug: str, session_id: str = "") -> None:
        """Mark a profile as suspended (window killed, session ID saved for resume)."""
        state = self.get_state(slug)
        state.window_id = ""
        if session_id:
            state.last_session_id = session_id
        self._save_states()

    def touch(self, slug: str) -> None:
        """Update last_active timestamp."""
        state = self.get_state(slug)
        state.last_active = time.time()
        self._save_states()

    def get_telegram_deep_link(self, slug: str) -> str | None:
        """Generate a Telegram deep link for a profile's topic.

        Only works for supergroups (negative chat_id). Returns None for
        private chats since Telegram deep links require a supergroup context.
        Falls back to session_manager's group_chat_ids if our stored chat_id
        is a private user ID.
        """
        from .session import session_manager

        state = self.states.get(slug)
        if not state or not state.topic_id:
            return None

        chat_id = state.chat_id

        # If we stored a positive ID (user's private chat), try to find
        # the real supergroup ID from session_manager
        if chat_id >= 0:
            for key, gid in session_manager.group_chat_ids.items():
                if gid < 0:  # It's a supergroup ID
                    chat_id = gid
                    break

        # Deep links only work with supergroup IDs (negative)
        if chat_id >= 0:
            return None

        # Convert: -100XXXXXXXXXX -> XXXXXXXXXX
        chat_id_str = str(abs(chat_id))
        if chat_id_str.startswith("100"):
            chat_id_str = chat_id_str[3:]
        return f"https://t.me/c/{chat_id_str}/{state.topic_id}"

    def find_by_topic(self, topic_id: int) -> Profile | None:
        """Find a profile by its Telegram topic ID."""
        for slug, state in self.states.items():
            if state.topic_id == topic_id:
                return self.profiles.get(slug)
        return None


# Singleton
profile_manager = ProfileManager()
