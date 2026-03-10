"""
Prompt loader — loads LLM prompts from YAML files.

Usage:
    from trustbot.prompts.loader import get_prompt

    content = get_prompt("agent.system_prompt")
    content = get_prompt("agent.validation_prompt", caller_function="foo", ...)

Language pack (detected by Agent 0) is automatically injected when prompts
use {language} or {language_pack}. Pass explicitly to override.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("trustbot.prompts")

_PROMPTS_DIR = Path(__file__).resolve().parent
_CACHE: dict[str, str] = {}


def _get_session_language_kwargs() -> dict[str, str]:
    """Inject language pack from session state for prompt formatting."""
    try:
        from trustbot.state.session import get_language_pack_str, get_primary_language
        return {
            "language": get_primary_language(),
            "language_pack": get_language_pack_str(),
        }
    except ImportError:
        return {"language": "unknown", "language_pack": "unknown"}


def get_prompt(prompt_id: str, **kwargs) -> str:
    """
    Load prompt by id, optionally format with kwargs.

    Prompt IDs use dot notation: agent.system_prompt, llm.neo4j_agent_system, etc.
    The id maps to prompts/<subdir>/<name>.yaml (e.g. agent/system_prompt.yaml).

    If kwargs are provided, the prompt content is formatted with str.format(**kwargs).
    Language pack (language, language_pack) from session state is merged in
    automatically unless explicitly passed in kwargs.
    """
    if prompt_id in _CACHE:
        content = _CACHE[prompt_id]
    else:
        content = _load_prompt(prompt_id)
        _CACHE[prompt_id] = content

    # Merge session language pack into kwargs (caller can override)
    format_kwargs = dict(_get_session_language_kwargs())
    format_kwargs.update(kwargs)

    if format_kwargs:
        try:
            return content.format(**format_kwargs)
        except KeyError as e:
            logger.warning("Missing template variable %s for prompt %s", e, prompt_id)
            return content
    return content


def _load_prompt(prompt_id: str) -> str:
    """Load prompt content from YAML file."""
    try:
        import yaml
    except ImportError:
        raise ImportError("PyYAML is required for prompt loading. Install with: pip install pyyaml")

    parts = prompt_id.split(".")
    if len(parts) < 2:
        raise ValueError(f"Invalid prompt_id: {prompt_id}. Use format: agent.system_prompt")

    subdir = parts[0]
    name = "_".join(parts[1:])
    yaml_path = _PROMPTS_DIR / subdir / f"{name}.yaml"

    if not yaml_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {yaml_path}")

    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data:
        raise ValueError(f"Empty prompt file: {yaml_path}")

    content = data.get("content") or data.get("prompt") or ""
    if isinstance(content, str):
        return content.strip()
    raise ValueError(f"Prompt {prompt_id}: content must be a string")
