"""Local Chroma compatibility checks."""

from __future__ import annotations

import pytest

import retriever


def test_mismatched_index_meta_has_reset_instruction(monkeypatch):
    monkeypatch.setattr(
        retriever.index_build,
        "_read_index_meta",
        lambda: {"embed_model": "old-model", "embed_dim": 1536},
    )

    with pytest.raises(RuntimeError, match=r"uv run python index_build\.py --reset"):
        retriever._check_index_meta()
