from trustbot.tools.base import BaseTool, ToolRegistry

__all__ = ["BaseTool", "ToolRegistry"]


def get_neo4j_tool():
    from trustbot.tools.neo4j_tool import Neo4jTool
    return Neo4jTool


def get_filesystem_tool():
    from trustbot.tools.filesystem_tool import FilesystemTool
    return FilesystemTool


def get_index_tool():
    from trustbot.tools.index_tool import IndexTool
    return IndexTool
