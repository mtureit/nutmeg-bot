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


def test_splits_on_headings():
    chunks = chunking.chunk_markdown(SAMPLE)
    assert len(chunks) == 4
    assert chunks[0]["heading_path"] == ["タイトル"]
    assert chunks[1]["heading_path"] == ["タイトル", "第1節"]
    assert chunks[2]["heading_path"] == ["タイトル", "第1節", "小見出し"]
    assert chunks[3]["heading_path"] == ["タイトル", "第2節"]


def test_heading_text_stays_in_chunk():
    chunks = chunking.chunk_markdown(SAMPLE)
    assert chunks[1]["text"].startswith("## 第1節")


def test_deterministic():
    first = chunking.chunk_markdown(SAMPLE)
    second = chunking.chunk_markdown(SAMPLE)
    assert first == second


def test_long_section_is_split_with_overlap():
    body = "\n\n".join(f"段落{i}" * 40 for i in range(20))
    md = f"# 長いページ\n\n{body}\n"
    chunks = chunking.chunk_markdown(md, max_chars=500, overlap=50)

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
