#!/usr/bin/env python3
"""Validate agent plugin.json and service-provider provider.json manifests.

Errors (fail CI):
  - a manifest is not valid JSON
  - an agent plugin is missing plugin_id, type, or display_name
  - a service provider is missing provider_code (or code) or display_name

Warnings (do not fail CI, surfaced as GitHub annotations):
  - an agent plugin has no entrypoint, or its entrypoint file is missing
  - plugin_id does not match its directory name

Run from the repository root:  python3 .github/scripts/validate_plugins.py
"""
from __future__ import annotations

import json
import os
import sys
from glob import glob

errors = 0
warnings = 0


def err(path: str, msg: str) -> None:
    global errors
    errors += 1
    print(f"::error file={path}::{msg}")
    print(f"  ERROR  {path}: {msg}")


def warn(path: str, msg: str) -> None:
    global warnings
    warnings += 1
    print(f"::warning file={path}::{msg}")
    print(f"  warn   {path}: {msg}")


def load(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        err(path, f"invalid JSON: {exc}")
    except OSError as exc:
        err(path, f"cannot read: {exc}")
    return None


def check_agent(path: str) -> None:
    data = load(path)
    if data is None:
        return
    directory = os.path.dirname(path)
    dir_name = os.path.basename(directory)

    for field in ("plugin_id", "type", "display_name"):
        if not data.get(field):
            err(path, f"missing required field: {field}")

    plugin_id = data.get("plugin_id")
    if plugin_id and plugin_id != dir_name:
        warn(path, f"plugin_id '{plugin_id}' does not match directory '{dir_name}'")

    entrypoint = data.get("entrypoint")
    if not entrypoint:
        warn(path, "no entrypoint declared")
    elif not os.path.isfile(os.path.join(directory, entrypoint)):
        warn(path, f"entrypoint file not found: {entrypoint}")


def check_provider(path: str) -> None:
    data = load(path)
    if data is None:
        return
    for field in ("code", "name", "auth_type"):
        if not data.get(field):
            err(path, f"missing required field: {field}")


def main() -> int:
    agents = sorted(glob("ai-agents/*/plugin.json"))
    providers = sorted(glob("service-providers/*/provider.json"))

    print(f"Validating {len(agents)} agent plugins and {len(providers)} service providers\n")

    for path in agents:
        check_agent(path)
    for path in providers:
        check_provider(path)

    print(f"\n{len(agents) + len(providers)} manifests checked: "
          f"{errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
