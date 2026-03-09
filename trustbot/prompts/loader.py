"""
Prompt loader — loads LLM prompts from YAML files.

Usage:
    from trustbot.prompts.loader import get_prompt

    content = get_prompt("agent.system_prompt")
    content = get_prompt("agent.validation_prompt", caller_function="foo", ...)
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("trustbot.prompts")

_PROMPTS_DIR = Path(__file__).resolve().parent
_CACHE: dict[str, str] = {}


def get_prompt(prompt_id: str, **kwargs) -> str:
    """
    Load prompt by id, optionally format with kwargs.

    Prompt IDs use dot notation: agent.system_prompt, llm.neo4j_agent_system, etc.
    The id maps to prompts/<subdir>/<name>.yaml (e.g. agent/system_prompt.yaml).

    If kwargs are provided, the prompt content is formatted with str.format(**kwargs).
    """
    if prompt_id in _CACHE:
        content = _CACHE[prompt_id]
    else:
        content = _load_prompt(prompt_id)
        _CACHE[prompt_id] = content

    if kwargs:
        try:
            return content.format(**kwargs)
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
