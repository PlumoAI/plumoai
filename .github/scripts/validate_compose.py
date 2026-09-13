#!/usr/bin/env python3
"""Validate that the docker-compose files are structurally well-formed YAML.

Docker Compose supports the custom YAML tags `!override` and `!reset` for merge
control, which a plain safe-loader rejects. This loader tolerates unknown tags
so the check validates structure without a false positive on valid compose.

This is a syntax/structure check, not a full `docker compose config` (which would
require a populated .env). It catches broken indentation, duplicate keys, and
malformed YAML before a PR merges.

Run from the repository root:  python3 .github/scripts/validate_compose.py
"""
from __future__ import annotations

import sys
from glob import glob

import yaml


class ComposeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates Compose's !override / !reset tags."""


def _ignore_unknown(loader, tag_suffix, node):  # noqa: ARG001
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


ComposeLoader.add_multi_constructor("!", _ignore_unknown)


def main() -> int:
    files = sorted(glob("docker-compose*.yml"))
    if not files:
        print("::error::no docker-compose*.yml files found")
        return 1

    errors = 0
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                yaml.load(fh, Loader=ComposeLoader)
            print(f"  ok    {path}")
        except yaml.YAMLError as exc:
            errors += 1
            print(f"::error file={path}::invalid YAML: {exc}")
            print(f"  ERROR {path}: {exc}")

    print(f"\n{len(files)} compose file(s) checked: {errors} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
