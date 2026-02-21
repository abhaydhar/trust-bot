"""
TrustBot application entry point.

Initializes all tools and launches the UI.
Code indexing is now done via the Git Indexer tab in the UI.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

import gradio as gr

from trustbot.config import settings
from trustbot.tools.base import ToolRegistry
from trustbot.tools.filesystem_tool import FilesystemTool
from trustbot.tools.neo4j_tool import Neo4jTool
from trustbot.ui.app import create_ui

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("trustbot")


async def initialize_app() -> ToolRegistry:
    """Initialize all tools and return the registry."""
    registry = ToolRegistry()

    # Register tools
    neo4j_tool = Neo4jTool()
    fs_tool = FilesystemTool()

    registry.register(neo4j_tool)
    registry.register(fs_tool)

    # Optional: IndexTool (ChromaDB) - may fail on Python 3.14
    try:
        from trustbot.tools.index_tool import IndexTool
        index_tool = IndexTool()
        registry.register(index_tool)
        logger.info("Index tool registered (ChromaDB)")
    except Exception as e:
        logger.warning("Index tool skipped (ChromaDB error: %s)", str(e)[:100])

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

    return registry


def main() -> None:
    """Main entry point — start TrustBot."""
    logger.info("Starting TrustBot v0.2.0 (agentic)")
    logger.info("Codebase root: %s", settings.codebase_root.resolve())
    logger.info("LLM model: %s", settings.litellm_model)

    # Use a single event loop for the entire application lifecycle
    # to avoid Windows ProactorEventLoop issues with Neo4j async driver
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        # Initialize tools
        registry = loop.run_until_complete(initialize_app())

        # Create the Gradio UI (code index will be created via Git Indexer tab)
        app = create_ui(registry)
        port = settings.server_port
        logger.info("UI built. Launching on http://localhost:%d ...", port)

        # Launch Gradio without blocking, then run the event loop manually
        # This prevents Windows ProactorEventLoop issues with Neo4j
        app.launch(
            server_name="127.0.0.1",
            server_port=port,
            share=False,
            prevent_thread_lock=True,
            inbrowser=False,
        )

        # Keep the event loop running to service Neo4j and other async operations
        logger.info("Server running. Press Ctrl+C to stop.")
        try:
            while True:
                loop.run_until_complete(asyncio.sleep(1))
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received.")
    finally:
        # Graceful shutdown
        try:
            loop.run_until_complete(registry.shutdown_all())
            logger.info("Tools shut down cleanly.")
        except Exception as e:
            logger.warning("Shutdown warning: %s", e)
        finally:
            loop.close()


if __name__ == "__main__":
    main()
