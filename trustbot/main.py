"""
TrustBot application entry point.

Initializes all tools, runs the indexing pipeline, and launches the UI.
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
from trustbot.tools.index_tool import IndexTool
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
    index_tool = IndexTool()

    registry.register(neo4j_tool)
    registry.register(fs_tool)
    registry.register(index_tool)

    # Initialize all tools (connects to Neo4j, sets up filesystem root, loads index)
    await registry.initialize_all()
    logger.info("All tools initialized successfully.")

    return registry


def main() -> None:
    """Main entry point — start TrustBot."""
    logger.info("Starting TrustBot v0.1.0")
    logger.info("Codebase root: %s", settings.codebase_root.resolve())
    logger.info("LLM model: %s", settings.litellm_model)

    # Initialize tools
    registry = asyncio.run(initialize_app())

    try:
        # Create and launch the Gradio UI
        app = create_ui(registry)
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
