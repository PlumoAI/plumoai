from __future__ import annotations

import importlib.util
import os
from typing import Any


def project_root_from(start_file: str) -> str:
    """
    Resolve project root from a file inside ai-agents/<plugin>/...:
    ai-agents/<plugin>/<file>.py -> project root
    """
    return os.path.abspath(os.path.join(os.path.dirname(start_file), "..", ".."))
