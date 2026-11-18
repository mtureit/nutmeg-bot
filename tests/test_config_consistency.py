"""Python and Worker embedding settings must stay aligned."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import config


def test_worker_embedding_dimensions_match_python_config():
    source = (Path(__file__).parents[1] / "worker/src/config.ts").read_text(encoding="utf-8")
    match = re.search(r"export const EMBED_DIMENSIONS\s*=\s*(\d+)\s*;", source)

    assert match is not None, "worker/src/config.ts must define EMBED_DIMENSIONS"
    assert int(match.group(1)) == config.EMBED_DIM


def _python_list(source: str, name: str) -> list[str]:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                value = node.value
                parsed = ast.literal_eval(value)
                assert isinstance(parsed, list)
                assert all(isinstance(item, str) for item in parsed)
                return parsed
    raise AssertionError(f"{name} must be a literal list in config.py")


def _worker_list(source: str, name: str) -> list[str]:
    match = re.search(
        rf"export const {name}\s*=\s*\[(?P<body>.*?)\](?:\s+as const)?\s*;",
        source,
        re.S,
    )
    assert match is not None, f"worker/src/config.ts must define {name} as a literal list"
    return re.findall(r"[\"']([^\"']+)[\"']", match.group("body"))


def test_worker_exclusions_match_python_config():
    root = Path(__file__).parents[1]
    python_source = (root / "config.py").read_text(encoding="utf-8")
    worker_source = (root / "worker/src/config.ts").read_text(encoding="utf-8")

    normalize = lambda value: value.replace("-", "").casefold()
    python_ids = {normalize(value) for value in _python_list(python_source, "NOTION_EXCLUDE_PAGE_IDS")}
    worker_ids = {normalize(value) for value in _worker_list(worker_source, "EXCLUDED_NOTION_IDS")}
    assert worker_ids == python_ids


def test_worker_title_exclusions_match_python_config():
    root = Path(__file__).parents[1]
    python_source = (root / "config.py").read_text(encoding="utf-8")
    worker_source = (root / "worker/src/config.ts").read_text(encoding="utf-8")
    assert _worker_list(worker_source, "EXCLUDE_TITLE_KEYWORDS") == _python_list(
        python_source, "EXCLUDE_TITLE_KEYWORDS"
    )
