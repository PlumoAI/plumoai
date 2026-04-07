"""
Shared helpers for tool plugins.

Note:
  - In this repo the plugins live under the `ai-agents/` folder.
  - At runtime, Docker Compose mounts that folder into the `ai-service` container at
    `/opt/plumoai/app_agents`, so the import path remains `app_agents.*`.

This package exists so plugin entrypoints can import:
    from app_agents._shared.llm_tools_loader import ...
without relying on implicit namespace packages.
"""

