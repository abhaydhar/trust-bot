"""
Session state for language pack (tech stack) detected by Agent 0.

Agent 0 runs during indexing and detects languages in the codebase (e.g. python,
java, typescript). This module-level state is set once and read by all
downstream agents for language-aware prompts.
"""

from __future__ import annotations

# Detected languages from Agent 0 — set during indexing, read by agents
_language_pack: list[str] = []


def set_language_pack(languages: list[str]) -> None:
    """Set the detected language pack (tech stack) from Agent 0.

    Called after Agent 0 completes during indexing. Persists for the session.
    """
    global _language_pack
    _language_pack = list(languages) if languages else []


def get_language_pack() -> list[str]:
    """Return the detected language pack. Empty if Agent 0 has not run."""
    return list(_language_pack)


def get_primary_language() -> str:
    """Return the primary (first) detected language, or 'unknown' if none."""
    return _language_pack[0] if _language_pack else "unknown"


def get_language_pack_str() -> str:
    """Return comma-separated language pack for use in prompts."""
    return ", ".join(_language_pack) if _language_pack else "unknown"
