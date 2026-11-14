#!/usr/bin/env python3
"""
Read one dotted key from a PatchPilot install config — used by install-k3s.sh.

    python3 k8s/yaml_get.py <config.yaml> <dotted.key> [default]

Prints the value (booleans as true/false, lists one item per line) or the
default when the key is absent, empty or null. Exits 2 when the file cannot
be read or parsed — the installer must stop rather than silently install with
defaults.

Uses PyYAML when it is installed. Otherwise falls back to a small parser for
the subset install-config.yaml uses: nested mappings by indentation, scalars
(plain or quoted), `[]`/`[a, b]` inline lists, `- item` block lists and
`#` comments. The previous fallback flattened the file and so returned the
default for every nested key, which silently ignored the whole config on any
machine without PyYAML (e.g. Homebrew Python).
"""

import re
import sys


class ConfigError(Exception):
    pass


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(text: str):
    t = text.strip()
    if t == "" or t in ("~", "null", "Null", "NULL"):
        return None
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        return t[1:-1]
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        return [] if not inner else [_scalar(x) for x in inner.split(",")]
    if t in ("true", "True", "TRUE", "yes", "Yes"):
        return True
    if t in ("false", "False", "FALSE", "no", "No"):
        return False
    return t


_KEY_RE = re.compile(r"^([A-Za-z0-9_][\w.-]*)\s*:(?:\s+(.*))?$")


def parse_fallback(text: str) -> dict:
    lines = []
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ConfigError(f"line {n}: tab indentation is not valid YAML")
        s = _strip_comment(raw)
        if s.strip() and s.strip() != "---":
            lines.append((n, len(s) - len(s.lstrip(" ")), s.strip()))

    root: dict = {}
    stack: list = [(-1, root)]  # (indent of the owning key, container)
    for i, (n, indent, s) in enumerate(lines):
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if s == "-" or s.startswith("- "):
            if not isinstance(parent, list):
                raise ConfigError(f"line {n}: list item outside a list")
            parent.append(_scalar(s[1:]))
            continue

        m = _KEY_RE.match(s)
        if not m:
            raise ConfigError(f"line {n}: cannot parse {s!r}")
        if not isinstance(parent, dict):
            raise ConfigError(f"line {n}: key inside a list is not supported")
        key, rest = m.group(1), (m.group(2) or "").strip()
        if rest:
            parent[key] = _scalar(rest)
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if nxt and nxt[1] > indent:
            child = [] if (nxt[2] == "-" or nxt[2].startswith("- ")) else {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = None
    return root


def load(path: str) -> dict:
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}")
    try:
        import yaml  # noqa: WPS433
    except ImportError:
        data = parse_fallback(text)
    else:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ConfigError(f"invalid YAML in {path}: {e}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def get(data: dict, dotted: str):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def render(value, default: str) -> str:
    if value is None or value == "" or value == "null":
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "\n".join(str(x) for x in value)
    return str(value)


def main(argv: list) -> int:
    if len(argv) < 3:
        print("usage: yaml_get.py <config.yaml> <dotted.key> [default]", file=sys.stderr)
        return 2
    try:
        data = load(argv[1])
    except ConfigError as e:
        print(f"yaml_get: {e}", file=sys.stderr)
        return 2
    print(render(get(data, argv[2]), argv[3] if len(argv) > 3 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
