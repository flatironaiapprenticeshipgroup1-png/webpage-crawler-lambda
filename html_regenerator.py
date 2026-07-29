import html as html_lib
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional
from urllib.parse import urljoin

import openai
from openai import OpenAI
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

MAX_CHARS_PER_CHUNK = 30_000
_CHARS_HARD_LIMIT = 300_000  # ~100k tokens at 3 chars/token — well under 128k context

_NUMERIC_ENTITY_RE = re.compile(r"&#x?[0-9a-fA-F]+;")


def _unescape_numeric_entities(text: str) -> str:
    """
    Unescapes only numeric character references (e.g. &#128512;), which is how
    models sometimes encode emoji despite instructions not to. Unlike html.unescape,
    this leaves named entities like &quot; and &amp; alone so attribute values
    that are legitimately escaped don't get corrupted into malformed markup.
    """
    return _NUMERIC_ENTITY_RE.sub(lambda m: html_lib.unescape(m.group(0)), text)


_LEADING_CODE_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*[ \t]*\n")
_TRAILING_CODE_FENCE_RE = re.compile(r"\n?[ \t]*```\s*$")


def _strip_code_fences(text: str) -> str:
    """
    Strips a leading ```html-style fence and/or trailing ``` fence the model
    added despite being told not to. Otherwise the fence markers get spliced
    into the document as literal text, since chunk output is inserted as-is.
    The two fences are stripped independently so a response truncated at
    max_tokens before the model closed its fence still has the leading one
    removed.
    """
    if not text:
        return text
    stripped = _LEADING_CODE_FENCE_RE.sub("", text, count=1)
    stripped = _TRAILING_CODE_FENCE_RE.sub("", stripped, count=1)
    return stripped

_HEAD_LINK_RELS_TO_STRIP = {
    "preload", "prefetch", "dns-prefetch", "preconnect",
    "canonical", "alternate", "manifest", "stylesheet",
}


def _split_node_into_parts(node, max_chars: int) -> list[str]:
    """
    Returns a list of HTML strings derived from node, each <= max_chars.
    Recursively descends into child nodes for oversized elements.
    Falls back to string truncation only for irreducible leaf nodes.
    """
    node_str = str(node)
    if len(node_str) <= max_chars:
        return [node_str]
    if isinstance(node, Tag) and node.name:
        parts: list[str] = []
        current: list[str] = []
        current_size = 0
        for child in node.children:
            for part in _split_node_into_parts(child, max_chars):
                if current and current_size + len(part) > max_chars:
                    parts.append("".join(current))
                    current = [part]
                    current_size = len(part)
                else:
                    current.append(part)
                    current_size += len(part)
        if current:
            parts.append("".join(current))
        return parts if parts else [node_str[:max_chars]]
    logger.warning("Leaf node too large to split (%d chars), truncating to %d", len(node_str), max_chars)
    return [node_str[:max_chars]]


def split_html_into_chunks(
    html: str,
    base_url: str = "",
    image_map: Optional[dict[str, str]] = None,
    max_chars: int = MAX_CHARS_PER_CHUNK,
) -> tuple[list[str], list[str], list[str]]:
    """
    Returns (chunks, labels, head_scripts) where labels are 'head' or 'body'.
    Chunk 0 is always the <head> element (with scripts/styles stripped);
    subsequent chunks are groups of top-level <body> children packed to max_chars each.
    head_scripts contains the original <script> tags to be reinserted after regeneration.
    Falls back to a single raw chunk if the document can't be parsed.

    image_map maps an image's absolute source URL to a cached local path (e.g.
    "./images/img-0.jpg"); images not present in the map fall back to their
    absolute original URL so they still render even if caching failed.
    """
    image_map = image_map or {}
    soup = BeautifulSoup(html, "html.parser")
    head = soup.find("head")
    body = soup.find("body")

    if not head or not body:
        return [html], ["raw"], []

    if base_url:
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if not src or src.startswith("data:"):
                continue
            absolute = urljoin(base_url, src)
            img["src"] = image_map.get(absolute, absolute)

    head_scripts = [str(tag) for tag in head.find_all("script")]
    for tag in head.find_all(["script", "style", "noscript"]):
        tag.decompose()

    # Strip link tags that serve no purpose after visual regeneration
    for tag in head.find_all("link"):
        rel = " ".join(tag.get("rel", [])).lower()
        if rel in _HEAD_LINK_RELS_TO_STRIP:
            tag.decompose()

    # Strip meta tags that are irrelevant to visual regeneration
    for tag in head.find_all("meta"):
        prop = tag.get("property", "")
        name = tag.get("name", "").lower()
        http_equiv = tag.get("http-equiv", "")
        if (
            prop.startswith("og:")
            or name.startswith("twitter:")
            or http_equiv
            or name == "robots"
        ):
            tag.decompose()

    head_str = str(head)
    if len(head_str) > max_chars:
        logger.warning(
            "Head element still exceeds %d chars after stripping (%d chars) — truncating",
            max_chars, len(head_str),
        )
        head_str = head_str[:max_chars]

    chunks = [head_str]
    labels = ["head"]

    current_parts: list[str] = []
    current_size = 0

    for child in body.children:
        for part in _split_node_into_parts(child, max_chars):
            if current_parts and current_size + len(part) > max_chars:
                chunks.append("".join(current_parts))
                labels.append("body")
                current_parts = [part]
                current_size = len(part)
            else:
                current_parts.append(part)
                current_size += len(part)

    if current_parts:
        chunks.append("".join(current_parts))
        labels.append("body")

    return chunks, labels, head_scripts


def _regenerate_chunk(
    client: OpenAI,
    chunk: str,
    label: str,
    theme: str,
    chunk_index: int,
    total_chunks: int,
) -> str:
    if len(chunk) > _CHARS_HARD_LIMIT:
        logger.warning(
            "[OpenAI] Chunk %d/%d exceeds hard limit (%d chars > %d), truncating",
            chunk_index + 1, total_chunks, len(chunk), _CHARS_HARD_LIMIT,
        )
        chunk = chunk[:_CHARS_HARD_LIMIT]

    theme_description = theme.strip() if theme and theme.strip() else (
        "a clean, modern redesign using current web design best practices"
    )

    if label == "head":
        system_msg = (
            f"You are an HTML transformation expert for a website theme regeneration system.\n\n"
            f"You will receive the contents of a <head> element. Transform it for the theme: {theme_description}\n\n"
            f"REQUIRED:\n"
            f"- Add exactly one stylesheet link, even if the original had none: "
            f"<link rel=\"stylesheet\" href=\"./Regenerated-Styles.css\">\n"
            f"- Remove ALL <style> blocks (inline CSS is handled by the regenerated stylesheet)\n"
            f"- Update <title> to reflect the theme\n\n"
            f"ALLOWED:\n"
            f"- Add or update meta description/keywords to match the theme\n"
            f"- Keep all other <head> content (charset, viewport, etc.) unchanged\n\n"
            f"RULES:\n"
            f"Return ONLY the inner contents of <head> — no <head> wrapper tags, "
            f"no explanations, no markdown, no code fences."
        )
    else:
        system_msg = (
            f"You are an expert web designer. Rebuild the following HTML fragment from scratch for the theme: {theme_description}\n\n"
            f"Make it look visually impressive and clearly different from the original. "
            f"Design choices should feel intentional and cohesive — bold where it counts, "
            f"restrained where it doesn't. Avoid anything clunky, chaotic, or generic.\n\n"
            f"You may use inline styles for layout only (flex, grid, position, width, height, gap, margin, padding). "
            f"Do not use inline styles for visual properties (colors, fonts, borders, shadows, backgrounds) — those are handled by CSS.\n\n"
            f"IMPORTANT: Preserve all existing class names and id attributes exactly as they are. "
            f"The CSS is generated separately and targets those selectors — if you rename them, the styling breaks.\n\n"
            f"IMPORTANT: <svg> elements and their descendants no longer carry classes — their classes were "
            f"intentionally removed upstream and replaced with inline style=\"...\" attributes. You may edit "
            f"those inline style attributes on svg elements/descendants to match the theme where it fits, but "
            f"never add class or id attributes to an <svg> or anything inside it, and never otherwise alter "
            f"the svg's structural markup (paths, viewBox, etc.).\n\n"
            f"IMPORTANT: Preserve every <img> src attribute exactly as given — do not invent, replace, or omit image URLs. "
            f"The src value of the <img> tags must stay byte-for-byte the same.\n\n"
            f"Preserve all factual content (product names, prices, key data). "
            f"Preserve all emojis and special Unicode characters exactly as they appear — do not convert them to HTML entities. "
            f"Return ONLY valid HTML. No explanations, no markdown, no code fences, no <html>/<head>/<body> tags."
        )

    user_msg = (
        f"This is chunk {chunk_index + 1} of {total_chunks}. "
        f"Regenerate this HTML:\n\n{chunk}"
    )

    logger.info(
        "[OpenAI] Sending HTML chunk %d/%d (%s) to gpt-4o | input_chars=%d | max_tokens=16384",
        chunk_index + 1, total_chunks, label, len(chunk),
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=16384,
        )
    except openai.RateLimitError:
        logger.error("[OpenAI] Rate limit hit on HTML chunk %d/%d", chunk_index + 1, total_chunks, exc_info=True)
        raise
    except openai.APITimeoutError:
        logger.error("[OpenAI] Request timed out on HTML chunk %d/%d", chunk_index + 1, total_chunks, exc_info=True)
        raise
    except openai.APIStatusError as e:
        logger.error(
            "[OpenAI] API error on HTML chunk %d/%d | status=%s | message=%s",
            chunk_index + 1, total_chunks, e.status_code, e.message, exc_info=True,
        )
        raise
    except openai.APIError:
        logger.error("[OpenAI] Unexpected API error on HTML chunk %d/%d", chunk_index + 1, total_chunks, exc_info=True)
        raise

    finish_reason = response.choices[0].finish_reason
    prompt_tokens = response.usage.prompt_tokens
    completion_tokens = response.usage.completion_tokens
    input_cost  = prompt_tokens     / 1_000_000 * 2.50
    output_cost = completion_tokens / 1_000_000 * 10.00

    logger.info(
        "[OpenAI] HTML chunk %d/%d complete | finish_reason=%s | prompt_tokens=%d | "
        "completion_tokens=%d | cost=$%.6f",
        chunk_index + 1, total_chunks, finish_reason, prompt_tokens, completion_tokens,
        input_cost + output_cost,
    )
    if finish_reason != "stop":
        logger.warning(
            "[OpenAI] HTML chunk %d/%d finish_reason='%s' — output may be truncated",
            chunk_index + 1, total_chunks, finish_reason,
        )

    return _unescape_numeric_entities(_strip_code_fences(response.choices[0].message.content))


def regenerate_html(
    client: OpenAI,
    html: str,
    theme: str,
    on_chunk_complete: Optional[Callable[[int, int], None]] = None,
    base_url: str = "",
    image_map: Optional[dict[str, str]] = None,
) -> str:
    """
    Regenerates HTML using GPT-4o, processing chunks in parallel.

    on_chunk_complete(chunk_index, total_chunks) is called (thread-safely by the
    caller's responsibility) after each chunk finishes — use it to publish
    per-chunk status updates from the handler.
    """
    chunks, labels, head_scripts = split_html_into_chunks(html, base_url=base_url, image_map=image_map)
    logger.info("Split HTML into %d chunk(s) for processing (stripped %d head script(s))", len(chunks), len(head_scripts))

    results: dict[int, str] = {}

    with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
        futures = {
            executor.submit(_regenerate_chunk, client, chunks[i], labels[i], theme, i, len(chunks)): i
            for i in range(len(chunks))
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
                if on_chunk_complete:
                    on_chunk_complete(idx, len(chunks))
            except Exception:
                logger.error("HTML chunk %d/%d failed", idx + 1, len(chunks), exc_info=True)
                raise

    if labels[0] == "raw":
        return results[0]

    head_content = results[0]
    if "Regenerated-Styles.css" not in head_content:
        logger.warning("[OpenAI] Regenerated head omitted the stylesheet link — inserting it")
        head_content += '\n<link rel="stylesheet" href="./Regenerated-Styles.css">'
    if "charset" not in head_content.lower():
        logger.warning("[OpenAI] Regenerated head omitted a charset declaration — inserting one")
        head_content = '<meta charset="utf-8">\n' + head_content
    body_parts = [results[i] for i in range(1, len(chunks))]
    scripts_block = ("\n" + "\n".join(head_scripts)) if head_scripts else ""
    return (
        f"<!DOCTYPE html>\n<html>\n"
        f"<head>{head_content}{scripts_block}</head>\n"
        f"<body>{''.join(body_parts)}</body>\n"
        f"</html>"
    )
