from __future__ import annotations

from .javascript_node import JavaScriptNode


def create_node() -> JavaScriptNode:
    """Entrypoint for the JavaScript node."""
    return JavaScriptNode()
