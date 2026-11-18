"""Keep the Python and Worker redaction implementations on one fixture."""

from __future__ import annotations

import json
from pathlib import Path

import redact


def test_redact_fixture_matches_python() -> None:
    path = Path(__file__).parent / "fixtures" / "redact_cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    for case in cases:
        actual, count = redact.redact(case["input"])
        assert actual == case["expected"], case["name"]
        assert count == case["count"], case["name"]
