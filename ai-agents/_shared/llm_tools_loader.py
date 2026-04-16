from __future__ import annotations

import importlib.util
import os
from typing import Any


def project_root_from(start_file: str) -> str:
    """
    Resolve project root from a file inside tool_plugins/<plugin>/...:
    tool_plugins/<plugin>/<file>.py -> project root
    """
    return os.path.abspath(os.path.join(os.path.dirname(start_file), "..", ".."))


def load_llm_tool_class(*, project_root: str, module_filename: str, class_name: str) -> Any:
    llm_tools_path = os.path.join(project_root, "llm_tools")
    module_path = os.path.join(llm_tools_path, module_filename)
    spec = importlib.util.spec_from_file_location(module_filename.replace(".py", ""), module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load spec for {module_filename}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    cls = getattr(mod, class_name, None)
    if cls is None:
        raise AttributeError(f"{class_name} not found in {module_filename}")
    return cls

