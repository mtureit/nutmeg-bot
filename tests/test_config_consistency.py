"""Python and Worker embedding settings must stay aligned."""

from __future__ import annotations

import re
from pathlib import Path

import config


def test_worker_embedding_dimensions_match_python_config():
    source = (Path(__file__).parents[1] / "worker/src/config.ts").read_text(encoding="utf-8")
    match = re.search(r"export const EMBED_DIMENSIONS\s*=\s*(\d+)\s*;", source)

    assert match is not None, "worker/src/config.ts must define EMBED_DIMENSIONS"
    assert int(match.group(1)) == config.EMBED_DIM
