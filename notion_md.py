"""Notion blocks to Markdown (PLAN.md 10).

This module is deliberately pure: it converts already-fetched block dicts and
never touches the network. Image captions arrive through a callback so that the
OpenAI dependency stays in vision_client.py.

Children are expected to be pre-fetched by prepare.py and attached to each block
under the "children" key.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

CaptionFn = Callable[[dict], str | None]

_INDENT = "  "


def block_deep_link(page_id: str, block_id: str) -> str:
    """Permanent link that jumps to a specific block (PLAN.md 10.1).

    Notion-hosted file URLs are presigned and expire in about an hour, so the
    block link is what we keep instead.
    """
    return (
        f"https://www.notion.so/{page_id.replace('-', '')}"
        f"#{block_id.replace('-', '')}"
    )


def rich_text_to_md(rich_text: Iterable[dict]) -> str:
    """Render Notion rich text, keeping bold / italic / code / links."""
    out: list[str] = []
    for span in rich_text or []:
        text = span.get("plain_text", "")
        if not text:
            continue
        annotations = span.get("annotations", {})

        # Code first: Notion renders code spans without other inline styling.
        if annotations.get("code"):
            text = f"`{text}`"
        else:
            if annotations.get("bold"):
                text = f"**{text}**"
            if annotations.get("italic"):
                text = f"*{text}*"
            if annotations.get("strikethrough"):
                text = f"~~{text}~~"

        href = span.get("href")
        if href:
            text = f"[{text}]({href})"
        out.append(text)
    return "".join(out)


def _plain(block: dict, key: str) -> str:
    return rich_text_to_md(block.get(key, {}).get("rich_text", []))


def _render_table(block: dict, depth: int) -> list[str]:
    rows = [
        child
        for child in block.get("children", [])
        if child.get("type") == "table_row"
    ]
    if not rows:
        return []

    def cells(row: dict) -> list[str]:
        return [
            rich_text_to_md(cell).replace("|", "\\|")
            for cell in row.get("table_row", {}).get("cells", [])
        ]

    pad = _INDENT * depth
    header = cells(rows[0])
    lines = [
        pad + "| " + " | ".join(header) + " |",
        pad + "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in rows[1:]:
        lines.append(pad + "| " + " | ".join(cells(row)) + " |")
    return lines


def _render_block(
    block: dict,
    *,
    page_id: str,
    caption_for: CaptionFn | None,
    depth: int,
    numbering: dict[int, int],
) -> list[str]:
    btype = block.get("type", "")
    pad = _INDENT * depth
    lines: list[str] = []

    if btype in ("heading_1", "heading_2", "heading_3"):
        level = int(btype[-1])
        lines.append(f"{'#' * level} {_plain(block, btype)}")

    elif btype == "paragraph":
        text = _plain(block, btype)
        lines.append(pad + text if text else "")

    elif btype == "bulleted_list_item":
        lines.append(f"{pad}- {_plain(block, btype)}")

    elif btype == "numbered_list_item":
        numbering[depth] = numbering.get(depth, 0) + 1
        lines.append(f"{pad}{numbering[depth]}. {_plain(block, btype)}")

    elif btype == "to_do":
        checked = block.get("to_do", {}).get("checked", False)
        mark = "x" if checked else " "
        lines.append(f"{pad}- [{mark}] {_plain(block, btype)}")

    elif btype == "quote":
        lines.append(f"{pad}> {_plain(block, btype)}")

    elif btype == "callout":
        # Dashboard pages use empty callouts as frames around headings and
        # databases; a bare ">" line would only add noise to the chunk.
        text = _plain(block, btype)
        if text:
            icon = block.get("callout", {}).get("icon") or {}
            emoji = icon.get("emoji", "") if icon.get("type") == "emoji" else ""
            prefix = f"{emoji} " if emoji else ""
            lines.append(f"{pad}> {prefix}{text}")

    elif btype == "code":
        language = block.get("code", {}).get("language", "")
        body = rich_text_to_md(block.get("code", {}).get("rich_text", []))
        lines.append(f"{pad}```{language}")
        lines.extend(pad + line for line in body.splitlines())
        lines.append(f"{pad}```")

    elif btype == "toggle":
        lines.append(f"{pad}- {_plain(block, btype)}")

    elif btype == "table":
        lines.extend(_render_table(block, depth))

    elif btype == "divider":
        lines.append(f"{pad}---")

    elif btype == "child_page":
        title = block.get("child_page", {}).get("title", "")
        lines.append(f"{pad}- [{title}]({block_deep_link(block['id'], block['id'])})")

    elif btype == "child_database":
        # Rows are ingested as pages of their own; here we only note the database.
        title = block.get("child_database", {}).get("title", "")
        lines.append(f"{pad}- データベース: {title}")

    elif btype == "image":
        lines.extend(_render_image(block, page_id=page_id, caption_for=caption_for, pad=pad))

    else:
        # Unsupported block types are skipped rather than guessed at.
        return []

    # Recurse into children. Tables already consumed theirs, and a numbered list
    # nested under another one restarts at 1, so children get a fresh counter.
    if btype not in ("table",) and block.get("children"):
        child_depth = depth if btype in ("heading_1", "heading_2", "heading_3") else depth + 1
        lines.extend(
            blocks_to_markdown_lines(
                block["children"],
                page_id=page_id,
                caption_for=caption_for,
                depth=child_depth,
            )
        )

    return lines


def _render_image(block: dict, *, page_id: str, caption_for: CaptionFn | None, pad: str) -> list[str]:
    """Inline the caption where the image sat (PLAN.md 10.5).

    Inlining rather than emitting a separate chunk keeps the description next to
    its surrounding prose, so a hit on either one carries the other into the
    prompt.
    """
    caption = caption_for(block) if caption_for else None
    if not caption:
        notion_caption = rich_text_to_md(block.get("image", {}).get("caption", []))
        caption = notion_caption or "(説明なし)"
    link = block_deep_link(page_id, block["id"])
    lines = [f"{pad}> 【図: {line}】" if i == 0 else f"{pad}> {line}"
             for i, line in enumerate(caption.splitlines() or [""])]
    lines.append(f"{pad}> 画像リンク: {link}")
    return lines


def blocks_to_markdown_lines(
    blocks: Iterable[dict],
    *,
    page_id: str,
    caption_for: CaptionFn | None = None,
    depth: int = 0,
) -> list[str]:
    numbering: dict[int, int] = {}
    lines: list[str] = []
    for block in blocks:
        if block.get("type") != "numbered_list_item":
            numbering.pop(depth, None)
        lines.extend(
            _render_block(
                block,
                page_id=page_id,
                caption_for=caption_for,
                depth=depth,
                numbering=numbering,
            )
        )
    return lines


def _date_to_text(date: dict | None) -> str:
    if not date or not date.get("start"):
        return ""
    start = date["start"]
    end = date.get("end")
    return f"{start} 〜 {end}" if end else start


def property_value_to_text(prop: dict) -> str:
    """Render one database property value as plain text ("" when empty).

    Relations and rollups are skipped: they carry page ids, not readable text.
    Title is skipped too, since it already becomes the page heading.
    """
    ptype = prop.get("type", "")
    value = prop.get(ptype)

    if ptype == "rich_text":
        return rich_text_to_md(value or [])
    if ptype == "number":
        return "" if value is None else str(value)
    if ptype in ("select", "status"):
        return (value or {}).get("name", "")
    if ptype == "multi_select":
        return ", ".join(option.get("name", "") for option in value or [])
    if ptype == "date":
        return _date_to_text(value)
    if ptype == "checkbox":
        return "はい" if value else ""
    if ptype in ("url", "email", "phone_number"):
        return value or ""
    if ptype == "people":
        return ", ".join(person.get("name", "") for person in value or [] if person.get("name"))
    if ptype == "files":
        return ", ".join(item.get("name", "") for item in value or [] if item.get("name"))
    if ptype == "formula":
        inner = value or {}
        kind = inner.get("type", "")
        if kind == "date":
            return _date_to_text(inner.get("date"))
        if kind == "boolean":
            return "はい" if inner.get("boolean") else ""
        result = inner.get(kind)
        return "" if result is None else str(result)
    return ""


def properties_to_markdown_lines(properties: dict) -> list[str]:
    """Render a database row's properties as "- name: value" lines.

    Empty values are left out so a sparsely filled row does not turn into a
    list of bare labels.
    """
    lines: list[str] = []
    for name, prop in (properties or {}).items():
        if prop.get("type") == "title":
            continue
        text = property_value_to_text(prop)
        if text:
            label = name or "（無名の項目）"
            lines.append(f"- {label}: {text}")
    return lines


def blocks_to_markdown(
    blocks: Iterable[dict],
    *,
    page_id: str,
    title: str = "",
    caption_for: CaptionFn | None = None,
    properties: dict | None = None,
) -> str:
    """Convert a page's blocks into a Markdown document.

    For a database row, pass its properties: they are placed right under the
    title, so they land in the same chunk as the page name.
    """
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
        lines.append("")
    prop_lines = properties_to_markdown_lines(properties) if properties else []
    if prop_lines:
        lines.extend(prop_lines)
        lines.append("")
    lines.extend(
        blocks_to_markdown_lines(blocks, page_id=page_id, caption_for=caption_for)
    )

    # Collapse runs of blank lines so chunk boundaries stay predictable.
    out: list[str] = []
    for line in lines:
        if not line.strip() and out and not out[-1].strip():
            continue
        out.append(line.rstrip())
    return "\n".join(out).strip() + "\n"
