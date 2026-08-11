#!/usr/bin/env python3
"""Insert the Storydex feedback include into the matching nginx server block."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


def install_include(
    config_text: str,
    include_path: str,
    server_name: str = "updates.septemc.com",
) -> str:
    directive = f"include {include_path};"
    if directive in config_text:
        return config_text
    for server_match in re.finditer(r"\bserver\s*\{", config_text):
        depth = 1
        cursor = server_match.end()
        while cursor < len(config_text) and depth:
            if config_text[cursor] == "{":
                depth += 1
            elif config_text[cursor] == "}":
                depth -= 1
            cursor += 1
        if depth:
            break
        block = config_text[server_match.start():cursor]
        if re.search(rf"\bserver_name\b[^;]*\b{re.escape(server_name)}\b", block):
            insertion = cursor - 1
            return config_text[:insertion] + f"    {directive}\n" + config_text[insertion:]
    raise ValueError(f"{server_name} server block not found")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("include_path")
    parser.add_argument("--server-name", default="updates.septemc.com")
    args = parser.parse_args()
    original = args.config.read_text(encoding="utf-8")
    updated = install_include(original, args.include_path, args.server_name)
    args.config.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
