"""
Session state — persists across agents within a TrustBot run.

Agent 0 (Language Profiler) identifies the language/tech stack of the git repo
during indexing. This state is stored here and used by downstream agents to
fetch language-aware prompts and tailor analysis.
"""

from trustbot.state.session import (
    get_language_pack,
    get_primary_language,
    set_language_pack,
)

__all__ = [
    "get_language_pack",
    "get_primary_language",
    "set_language_pack",
]
