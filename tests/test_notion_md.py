"""Notion block -> Markdown conversion, tested without network access.

This is the part PLAN.md calls the hardest: nested blocks and rich-text
annotations. Keeping the converter pure is what makes these tests possible.
"""

from __future__ import annotations

import notion_md

PAGE_ID = "11112222333344445555666677778888"


def rt(text: str, **annotations):
    base = {
        "bold": False,
        "italic": False,
        "strikethrough": False,
        "underline": False,
        "code": False,
        "color": "default",
    }
    base.update(annotations)
    return {"plain_text": text, "annotations": base, "href": annotations.get("href")}


def block(btype: str, **payload):
    body = payload.pop("body", {})
    return {"id": payload.pop("id", "block-1"), "type": btype, btype: body, **payload}


def test_rich_text_annotations():
    spans = [
        rt("普通の"),
        rt("太字", bold=True),
        rt("斜体", italic=True),
        rt("コード", code=True),
    ]
    assert notion_md.rich_text_to_md(spans) == "普通の**太字***斜体*`コード`"


def test_rich_text_link():
    span = {
        "plain_text": "リンク",
        "annotations": {"bold": False, "italic": False, "code": False, "strikethrough": False},
        "href": "https://example.com",
    }
    assert notion_md.rich_text_to_md([span]) == "[リンク](https://example.com)"


def test_headings_and_paragraph():
    blocks = [
        block("heading_1", body={"rich_text": [rt("見出し1")]}),
        block("paragraph", body={"rich_text": [rt("本文")]}),
    ]
    md = notion_md.blocks_to_markdown(blocks, page_id=PAGE_ID)
    assert "# 見出し1" in md
    assert "本文" in md


def test_numbered_list_increments():
    blocks = [
        block("numbered_list_item", id="a", body={"rich_text": [rt("一つ目")]}),
        block("numbered_list_item", id="b", body={"rich_text": [rt("二つ目")]}),
        block("numbered_list_item", id="c", body={"rich_text": [rt("三つ目")]}),
    ]
    md = notion_md.blocks_to_markdown(blocks, page_id=PAGE_ID)
    assert "1. 一つ目" in md
    assert "2. 二つ目" in md
    assert "3. 三つ目" in md


def test_to_do_checked_state():
    blocks = [
        block("to_do", id="a", body={"rich_text": [rt("done")], "checked": True}),
        block("to_do", id="b", body={"rich_text": [rt("todo")], "checked": False}),
    ]
    md = notion_md.blocks_to_markdown(blocks, page_id=PAGE_ID)
    assert "- [x] done" in md
    assert "- [ ] todo" in md


def test_nested_toggle_children_are_indented():
    child = block("paragraph", id="child", body={"rich_text": [rt("子の本文")]})
    parent = block(
        "toggle",
        id="parent",
        body={"rich_text": [rt("トグル")]},
        children=[child],
    )
    md = notion_md.blocks_to_markdown([parent], page_id=PAGE_ID)
    assert "- トグル" in md
    assert "  子の本文" in md


def test_table_renders_markdown_table():
    def row(cells):
        return block("table_row", id=f"r{cells[0][0]['plain_text']}", body={"cells": cells})

    table = block(
        "table",
        id="t",
        body={"table_width": 2},
        children=[
            row([[rt("項目")], [rt("値")]]),
            row([[rt("定員")], [rt("100")]]),
        ],
    )
    md = notion_md.blocks_to_markdown([table], page_id=PAGE_ID)
    assert "| 項目 | 値 |" in md
    assert "| --- | --- |" in md
    assert "| 定員 | 100 |" in md


def test_image_caption_is_inlined_with_block_link():
    image = block(
        "image",
        id="aaaabbbbccccdddd",
        body={"type": "file", "file": {"url": "https://x/y.png"}, "caption": []},
    )
    md = notion_md.blocks_to_markdown(
        [image], page_id=PAGE_ID, caption_for=lambda _b: "図表。会場レイアウトを示す。"
    )
    assert "【図: 図表。会場レイアウトを示す。】" in md
    assert "画像リンク: https://www.notion.so/" in md
    assert "#aaaabbbbccccdddd" in md


def test_image_without_caption_still_recorded():
    image = block(
        "image",
        id="img2",
        body={"type": "file", "file": {"url": "https://x/y.png"}, "caption": []},
    )
    md = notion_md.blocks_to_markdown([image], page_id=PAGE_ID, caption_for=lambda _b: None)
    assert "【図: (説明なし)】" in md


def test_unsupported_block_is_skipped():
    blocks = [
        block("unsupported_thing", body={}),
        block("paragraph", body={"rich_text": [rt("残る")]}),
    ]
    md = notion_md.blocks_to_markdown(blocks, page_id=PAGE_ID)
    assert "残る" in md


def test_block_deep_link_strips_dashes():
    link = notion_md.block_deep_link("aaaa-bbbb", "cccc-dddd")
    assert link == "https://www.notion.so/aaaabbbb#ccccdddd"
