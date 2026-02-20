"""
TrustBot application entry point.

Initializes all tools, code index, runs the indexing pipeline, and launches the UI.
Supports both legacy single-agent and new multi-agent validation pipelines.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

import gradio as gr

from trustbot.config import settings
from trustbot.index.code_index import CodeIndex
from trustbot.tools.base import ToolRegistry
from trustbot.tools.filesystem_tool import FilesystemTool
from trustbot.tools.index_tool import IndexTool
from trustbot.tools.neo4j_tool import Neo4jTool
from trustbot.ui.app import create_ui

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("trustbot")


async def initialize_app() -> tuple[ToolRegistry, CodeIndex]:
    """Initialize all tools, code index, and return the registry."""
    registry = ToolRegistry()

    # Register tools
    neo4j_tool = Neo4jTool()
    fs_tool = FilesystemTool()
    index_tool = IndexTool()

    registry.register(neo4j_tool)
    registry.register(fs_tool)
    registry.register(index_tool)

    # Optional: Browser tool for E2E testing (disabled by default to avoid launching browser)
    if settings.enable_browser_tool:
        try:
            from trustbot.tools.browser_tool import BrowserTool
            browser_tool = BrowserTool()
            registry.register(browser_tool)
            logger.info("Browser tool registered (Playwright available)")
        except ImportError:
            logger.debug("Browser tool skipped (Playwright not installed)")

    # Initialize all tools
    await registry.initialize_all()
    logger.info("All tools initialized successfully.")

    # Build Code Index for multi-agent pipeline
    code_index = CodeIndex()
    try:
        stats = code_index.build()
        logger.info("Code index built: %d functions from %d files", stats["functions"], stats["files"])
    except Exception as e:
        logger.warning("Code index build failed (multi-agent validation may be limited): %s", e)

    return registry, code_index


def main() -> None:
    """Main entry point — start TrustBot."""
    logger.info("Starting TrustBot v0.2.0 (agentic)")
    logger.info("Codebase root: %s", settings.codebase_root.resolve())
    logger.info("LLM model: %s", settings.litellm_model)

    # Initialize tools and code index
    registry, code_index = asyncio.run(initialize_app())

    try:
        # Create and launch the Gradio UI
        app = create_ui(registry, code_index)
        port = settings.server_port
        logger.info("UI built. Launching on http://localhost:%d ...", port)
        app.launch(
            server_name="127.0.0.1",
            server_port=port,
            share=False,
        )
    finally:
        # Graceful shutdown: close Neo4j and other connections before exit.
        # Reduces AttributeError during asyncio teardown on Windows.
        try:
            asyncio.run(registry.shutdown_all())
            logger.info("Tools shut down cleanly.")
        except Exception as e:
            logger.warning("Shutdown warning: %s", e)


if __name__ == "__main__":
    main()
