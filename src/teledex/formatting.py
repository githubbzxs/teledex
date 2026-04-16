from __future__ import annotations

import html
import re
from typing import Callable


_CITATION_PATTERNS = [
    re.compile(r"【[^】]+†[^】]+】"),
    re.compile(r"\[\^?\d+\]"),
]


def strip_citations(text: str) -> str:
    cleaned = text
    for pattern in _CITATION_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    cleaned = re.sub(r"\n?来源[:：].*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def summarize_command(command: str, max_length: int = 60) -> str:
    command = command.strip().replace("\n", " ")
    if len(command) <= max_length:
        return command
    return command[: max_length - 3].rstrip() + "..."


def extract_first_bold_markdown(text: str) -> str | None:
    normalized = strip_citations(text).strip()
    if not normalized:
        return None
    for pattern in _STRONG_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        extracted = " ".join(match.group(1).split()).strip()
        if extracted:
            return extracted
    return None


def preview_text_for_agent_message(text: str, max_length: int = 80) -> str:
    single_line = " ".join(text.strip().split())
    if not single_line:
        return "正在整理回复..."
    if len(single_line) <= max_length:
        return single_line
    return single_line[: max_length - 3].rstrip() + "..."


_INLINE_CODE_PATTERN = re.compile(r"`([^`\n]+)`")
_LINK_PATTERN = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")
_STRONG_PATTERNS = [
    re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", flags=re.DOTALL),
    re.compile(r"__(?=\S)(.+?)(?<=\S)__", flags=re.DOTALL),
]
_ITALIC_PATTERNS = [
    re.compile(r"(?<!\*)\*(?=\S)(.+?)(?<=\S)\*(?!\*)", flags=re.DOTALL),
    re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)", flags=re.DOTALL),
]
_STRIKE_PATTERN = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", flags=re.DOTALL)
_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_UNORDERED_LIST_PATTERN = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED_LIST_PATTERN = re.compile(r"^\s*(\d+)\.\s+(.*)$")


def split_markdown_message(text: str, max_length: int) -> list[str]:
    normalized = strip_citations(text).replace("\r\n", "\n").strip()
    if not normalized:
        return [""]

    blocks = _collect_markdown_blocks(normalized)
    parts: list[str] = []
    current = ""

    for block in blocks:
        block_parts = [block]
        if len(block) > max_length:
            block_parts = _split_oversized_block(block, max_length)

        for block_part in block_parts:
            candidate = block_part if not current else f"{current}\n\n{block_part}"
            if len(candidate) <= max_length:
                current = candidate
                continue
            if current:
                parts.append(current)
            current = block_part

    if current:
        parts.append(current)
    return parts or [normalized]


def markdown_to_telegram_html(
    text: str,
    local_link_resolver: Callable[[str], str | None] | None = None,
) -> str:
    normalized = strip_citations(text).replace("\r\n", "\n").strip()
    if not normalized:
        return ""

    chunks: list[str] = []
    in_code_block = False
    code_lines: list[str] = []
    paragraph_lines: list[str] = []

    for line in normalized.split("\n"):
        if line.startswith("```"):
            if in_code_block:
                chunks.append(_render_code_block(code_lines))
                code_lines = []
                in_code_block = False
            else:
                if paragraph_lines:
                    chunks.append(_render_paragraph(paragraph_lines, local_link_resolver))
                    paragraph_lines = []
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if not line.strip():
            if paragraph_lines:
                chunks.append(_render_paragraph(paragraph_lines, local_link_resolver))
                paragraph_lines = []
            continue

        heading_match = _HEADING_PATTERN.match(line)
        if heading_match:
            if paragraph_lines:
                chunks.append(_render_paragraph(paragraph_lines, local_link_resolver))
                paragraph_lines = []
            chunks.append(
                f"<b>{_render_inline(heading_match.group(1).strip(), local_link_resolver)}</b>"
            )
            continue

        unordered_match = _UNORDERED_LIST_PATTERN.match(line)
        if unordered_match:
            if paragraph_lines:
                chunks.append(_render_paragraph(paragraph_lines, local_link_resolver))
                paragraph_lines = []
            chunks.append(
                f"• {_render_inline(unordered_match.group(1).strip(), local_link_resolver)}"
            )
            continue

        ordered_match = _ORDERED_LIST_PATTERN.match(line)
        if ordered_match:
            if paragraph_lines:
                chunks.append(_render_paragraph(paragraph_lines, local_link_resolver))
                paragraph_lines = []
            chunks.append(
                f"{ordered_match.group(1)}. "
                f"{_render_inline(ordered_match.group(2).strip(), local_link_resolver)}"
            )
            continue

        if line.lstrip().startswith(">"):
            if paragraph_lines:
                chunks.append(_render_paragraph(paragraph_lines, local_link_resolver))
                paragraph_lines = []
            quote_text = re.sub(r"^\s*>\s?", "", line, count=1)
            chunks.append(f"&gt; {_render_inline(quote_text, local_link_resolver)}")
            continue

        paragraph_lines.append(line.strip())

    if in_code_block:
        chunks.append(_render_code_block(code_lines))
    elif paragraph_lines:
        chunks.append(_render_paragraph(paragraph_lines, local_link_resolver))

    html_text = "\n".join(chunks).strip()
    return html_text or html.escape(normalized)


def _collect_markdown_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    in_code_block = False
    code_lines: list[str] = []

    for line in text.split("\n"):
        if line.startswith("```"):
            if in_code_block:
                code_lines.append(line)
                blocks.append("\n".join(code_lines).strip())
                code_lines = []
                in_code_block = False
            else:
                if current:
                    blocks.append("\n".join(current).strip())
                    current = []
                code_lines = [line]
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if not line.strip():
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue

        if (
            _HEADING_PATTERN.match(line)
            or _UNORDERED_LIST_PATTERN.match(line)
            or _ORDERED_LIST_PATTERN.match(line)
            or line.lstrip().startswith(">")
        ):
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            blocks.append(line.strip())
            continue

        current.append(line.rstrip())

    if in_code_block and code_lines:
        blocks.append("\n".join(code_lines).strip())
    elif current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]


def _split_oversized_block(block: str, max_length: int) -> list[str]:
    lines = block.split("\n")
    if lines and lines[0].startswith("```"):
        return _split_fenced_code_block(lines, max_length)
    return _split_plain_text(block, max_length)


def _split_fenced_code_block(lines: list[str], max_length: int) -> list[str]:
    opening_fence = lines[0]
    closing_fence = "```"
    body_lines = lines[1:]
    if body_lines and body_lines[-1].startswith("```"):
        closing_fence = body_lines[-1]
        body_lines = body_lines[:-1]

    overhead = len(opening_fence) + len(closing_fence) + 3
    body_limit = max(1, max_length - overhead)
    body = "\n".join(body_lines)
    chunks = _split_plain_text(body, body_limit) or [""]
    return [f"{opening_fence}\n{chunk}\n{closing_fence}".strip() for chunk in chunks]


def _split_plain_text(text: str, max_length: int) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []

    parts: list[str] = []
    remaining = stripped
    while remaining:
        if len(remaining) <= max_length:
            parts.append(remaining)
            break
        chunk = remaining[:max_length]
        split_at = chunk.rfind("\n")
        if split_at < max_length // 2:
            split_at = chunk.rfind(" ")
        if split_at <= 0:
            split_at = max_length
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return [part for part in parts if part]


def _render_paragraph(
    lines: list[str],
    local_link_resolver: Callable[[str], str | None] | None = None,
) -> str:
    return _render_inline("\n".join(line.strip() for line in lines), local_link_resolver)


def _render_code_block(lines: list[str]) -> str:
    content = html.escape("\n".join(lines))
    if content:
        content += "\n"
    return f"<pre><code>{content}</code></pre>"


def _render_inline(
    text: str,
    local_link_resolver: Callable[[str], str | None] | None = None,
) -> str:
    code_placeholders: dict[str, str] = {}

    def replace_code(match: re.Match[str]) -> str:
        token = f"@@CODE{len(code_placeholders)}@@"
        code_placeholders[token] = f"<code>{html.escape(match.group(1))}</code>"
        return token

    with_code_tokens = _INLINE_CODE_PATTERN.sub(replace_code, text)
    rendered = html.escape(with_code_tokens)

    for _ in range(3):
        previous = rendered
        rendered = _LINK_PATTERN.sub(
            lambda match: _render_markdown_link(match, local_link_resolver),
            rendered,
        )
        rendered = _apply_wrapped_pattern(rendered, _STRONG_PATTERNS, "b")
        rendered = _STRIKE_PATTERN.sub(r"<s>\1</s>", rendered)
        rendered = _apply_wrapped_pattern(rendered, _ITALIC_PATTERNS, "i")
        if rendered == previous:
            break

    for token, replacement in code_placeholders.items():
        rendered = rendered.replace(token, replacement)
    return rendered


def _apply_wrapped_pattern(text: str, patterns: list[re.Pattern[str]], tag: str) -> str:
    rendered = text
    for pattern in patterns:
        rendered = pattern.sub(rf"<{tag}>\1</{tag}>", rendered)
    return rendered


def _render_markdown_link(
    match: re.Match[str],
    local_link_resolver: Callable[[str], str | None] | None = None,
) -> str:
    label = match.group(1)
    target = html.unescape(match.group(2)).strip()
    if not target:
        return label
    if target.startswith(("http://", "https://")):
        return f'<a href="{html.escape(target, quote=True)}">{label}</a>'
    if local_link_resolver is not None:
        resolved_target = local_link_resolver(target)
        if resolved_target:
            return f'<a href="{html.escape(resolved_target, quote=True)}">{label}</a>'
    return f"{label} <code>{html.escape(target)}</code>"
