"""
TrustBot application entry point.

Initializes all tools and launches the NiceGUI-based UI.
Code indexing is done via the Code Indexer tab in the UI.
"""

from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv

load_dotenv()

from nicegui import app, ui
from nicegui import background_tasks

from trustbot.config import settings

# Workaround for NiceGUI: when async event handlers run, NiceGUI may call
# app.on_startup() to schedule the coroutine. If the app has already started,
# that raises. Patch on_startup so we schedule the handler via background_tasks instead.
_original_on_startup = app.on_startup

def _patched_on_startup(handler):
    if app.is_started:
        # App already running — schedule the handler instead of registering for next startup
        if asyncio.iscoroutine(handler):
            background_tasks.create(handler, name="late_startup")
        elif asyncio.iscoroutinefunction(handler):
            background_tasks.create(handler(), name="late_startup")
        else:
            try:
                result = handler()
                if asyncio.iscoroutine(result):
                    background_tasks.create(result, name="late_startup")
            except Exception:
                raise
        return
    _original_on_startup(handler)


app.on_startup = _patched_on_startup
from trustbot.tools.base import ToolRegistry
from trustbot.tools.filesystem_tool import FilesystemTool
from trustbot.tools.neo4j_tool import Neo4jTool

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("trustbot")

_registry: ToolRegistry | None = None
_registry_ready = asyncio.Event()


async def initialize_app() -> ToolRegistry:
    """Initialize all tools and return the registry."""
    registry = ToolRegistry()

    neo4j_tool = Neo4jTool()
    fs_tool = FilesystemTool()

    registry.register(neo4j_tool)
    registry.register(fs_tool)

    try:
        from trustbot.tools.index_tool import IndexTool
        index_tool = IndexTool()
        registry.register(index_tool)
        logger.info("Index tool registered (ChromaDB)")
    except Exception as e:
        logger.warning("Index tool skipped (ChromaDB error: %s)", str(e)[:100])

    if settings.enable_browser_tool:
        try:
            from trustbot.tools.browser_tool import BrowserTool
            browser_tool = BrowserTool()
            registry.register(browser_tool)
            logger.info("Browser tool registered (Playwright available)")
        except ImportError:
            logger.debug("Browser tool skipped (Playwright not installed)")

    await registry.initialize_all()
    logger.info("All tools initialized successfully.")

    return registry


@app.on_startup
async def _startup():
    global _registry
    logger.info("Starting TrustBot v0.3.0 (NiceGUI)")
    logger.info("Codebase root: %s", settings.codebase_root.resolve())
    logger.info("LLM model: %s", settings.litellm_model)
    _registry = await initialize_app()
    _registry_ready.set()


@app.on_shutdown
async def _shutdown():
    if _registry:
        try:
            await _registry.shutdown_all()
            logger.info("Tools shut down cleanly.")
        except Exception as e:
            logger.warning("Shutdown warning: %s", e)


def get_registry() -> ToolRegistry:
    """Return the initialised tool registry (available after startup)."""
    if _registry is None:
        raise RuntimeError("Registry not initialised yet")
    return _registry


async def wait_for_registry() -> ToolRegistry:
    """Wait for the registry to be ready (handles reload race condition)."""
    await _registry_ready.wait()
    return _registry


# Import creates the NiceGUI page routes
from trustbot.ui.app import create_ui  # noqa: E402


def main() -> None:
    """Main entry point -- start TrustBot."""
    create_ui()
    port = settings.server_port
    if not settings.storage_secret:
        logger.warning(
            "TRUSTBOT_STORAGE_SECRET not set; session state will not persist across reconnect/lock. "
            "Set it in .env to enable state restore."
        )
    session_kwargs: dict | None = None
    if settings.storage_secret and settings.session_max_age_days > 0:
        session_kwargs = {"max_age": settings.session_max_age_days * 24 * 3600}
    logger.info("Launching NiceGUI on http://localhost:%d ...", port)
    ui.run(
        host="127.0.0.1",
        port=port,
        title="TrustBot",
        reload=True,
        show=False,
        storage_secret=settings.storage_secret or None,
        session_middleware_kwargs=session_kwargs,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
