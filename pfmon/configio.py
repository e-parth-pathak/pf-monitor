"""Minimal YAML subset loader (nested maps + scalars) — stdlib only.

Falls back to PyYAML when available. The subset is deliberately exactly
what config/tariff.yaml and config/meters.yaml use: maps nested by
indentation, scalar values (str/int/float/bool), comments.
"""
from __future__ import annotations

import os


def load_yaml(path: str) -> dict:
    try:  # prefer the real thing when installed
        import yaml  # type: ignore

        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except ImportError:
        pass
    return _load_subset(path)


def _load_subset(path: str) -> dict:
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            if ":" not in stripped:
                raise ValueError(f"{path}:{lineno}: expected 'key: value'")
            key, _, rest = stripped.partition(":")
            key = key.strip().strip('"')
            value = rest.strip()
            # strip inline comment (we never use '#' inside quoted strings
            # except at start; handle the quoted case first)
            if value.startswith('"'):
                end = value.find('"', 1)
                value = value[1:end] if end > 0 else value[1:]
                parsed: object = value
                has_value = True
            elif value:
                if " #" in value:
                    value = value.split(" #", 1)[0].rstrip()
                parsed = _scalar(value)
                has_value = True
            else:
                has_value = False
                parsed = None
            while stack and indent <= stack[-1][0]:
                stack.pop()
            parent = stack[-1][1]
            if has_value:
                parent[key] = parsed
            else:
                child: dict = {}
                parent[key] = child
                stack.append((indent, child))
    return root


def _scalar(s: str):
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def load_config(base_dir: str) -> tuple[dict, dict, dict]:
    """Returns (tariff, settings, meters)."""
    tariff = load_yaml(os.path.join(base_dir, "config", "tariff.yaml"))
    meters_doc = load_yaml(os.path.join(base_dir, "config", "meters.yaml"))
    return tariff, meters_doc.get("settings", {}), meters_doc.get("meters", {})
