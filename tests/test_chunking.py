"""Chunking must stay deterministic: incremental ingest depends on it.

If chunk_markdown produced different output for the same input, the weekly cron
would re-embed unchanged pages every run -- a failure that shows up as a quiet
cost increase rather than an error.
"""

from __future__ import annotations

import chunking

SAMPLE = """# タイトル

導入の段落です。

## 第1節

第1節の本文。

### 小見出し

小見出しの本文。

## 第2節

第2節の本文。
"""


def test_packs_consecutive_sections_until_target():
    md = "# ページ\n\n" + "A" * 30 + "\n\n## 日程\n\n" + "B" * 30 + "\n\n## 会場\n\n" + "C" * 30
    chunks = chunking.chunk_markdown(md, target_chars=100, max_chars=150, min_chars=0, overlap=20)

    assert len(chunks) == 2
    assert chunks[0]["heading_path"] == ["ページ"]
    assert chunks[0]["text"].startswith("# ページ")
    assert "## 日程" in chunks[0]["text"]
    assert chunks[1]["heading_path"] == ["ページ", "会場"]
    assert "## 会場" in chunks[1]["text"]


def test_tiny_title_and_properties_merge_with_first_body():
    md = "# 企画書\n\n- Date: 2026-10-08\n\n## 日程\n\n" + "本文 " * 20
    chunks = chunking.chunk_markdown(md, target_chars=50, max_chars=150, overlap=20)

    assert len(chunks) == 1
    assert chunks[0]["heading_path"] == ["企画書"]
    assert chunks[0]["text"].startswith("# 企画書")
    assert "- Date: 2026-10-08" in chunks[0]["text"]
    assert "## 日程" in chunks[0]["text"]


def test_heading_path_is_first_section_in_packed_chunk():
    chunks = chunking.chunk_markdown(SAMPLE, target_chars=80, max_chars=150, overlap=20)

    assert chunks[0]["heading_path"] == ["タイトル"]
    assert "## 第1節" in chunks[0]["text"]
    assert all(len(chunk["text"]) <= 150 for chunk in chunks)


def test_deterministic():
    first = chunking.chunk_markdown(SAMPLE)
    second = chunking.chunk_markdown(SAMPLE)
    assert first == second


def test_long_section_is_split_with_overlap():
    body = "\n\n".join(f"段落{i}" * 40 for i in range(20))
    md = f"# 長いページ\n\n{body}\n"
    chunks = chunking.chunk_markdown(md, max_chars=500, target_chars=500, overlap=50)

    assert len(chunks) > 1
    assert all(len(chunk["text"]) <= 500 for chunk in chunks)
    assert all(chunk["heading_path"] == ["長いページ"] for chunk in chunks)


def test_headings_inside_code_fences_are_not_boundaries():
    md = """# ページ

```python
# これはコメントであって見出しではない
x = 1
```

本文。
"""
    chunks = chunking.chunk_markdown(md)
    assert len(chunks) == 1


def test_overlap_must_be_smaller_than_max():
    try:
        chunking.chunk_markdown("# a\n\nbody", max_chars=100, overlap=100)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_empty_input():
    assert chunking.chunk_markdown("") == []
