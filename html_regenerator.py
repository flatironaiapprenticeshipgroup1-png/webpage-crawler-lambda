import logging
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


def split_html_into_chunks(html: str, base_url: str = "", max_chars: int = MAX_CHARS_PER_CHUNK) -> tuple[list[str], list[str], list[str]]:
    """
    Returns (chunks, labels, head_scripts) where labels are 'head' or 'body'.
    Chunk 0 is always the <head> element (with scripts/styles stripped);
    subsequent chunks are groups of top-level <body> children packed to max_chars each.
    head_scripts contains the original <script> tags to be reinserted after regeneration.
    Falls back to a single raw chunk if the document can't be parsed.
    """
    soup = BeautifulSoup(html, "html.parser")
    head = soup.find("head")
    body = soup.find("body")

    if not head or not body:
        return [html], ["raw"], []

    if base_url:
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if src and not src.startswith(("http://", "https://", "data:")):
                img["src"] = urljoin(base_url, src)

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

    if label == "head":
        system_msg = (
            f"You are an HTML transformation expert for a website theme regeneration system.\n\n"
            f"You will receive the contents of a <head> element. Transform it for the theme: {theme}\n\n"
            f"REQUIRED:\n"
            f"- Replace ALL <link rel=\"stylesheet\"> tags with exactly one: "
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
            f"You are an elite HTML and web design expert. Your job is to completely rebuild "
            f"a website's HTML so it looks STUNNING and completely different from the original.\n\n"
            f"You will receive an HTML fragment. Rebuild it from scratch for the theme: {theme}\n\n"
            f"MAKE IT LOOK COOL:\n"
            f"Build a visually impressive layout — think hero sections with bold headlines, "
            f"feature grids, layered card stacks, full-bleed sections, floating elements, "
            f"overlapping content, large typographic moments, and dramatic whitespace,\n"
            f"Add eye-catching decorative elements that fit the theme "
            f"(e.g. cyberpunk: glitch spans, scanline overlays, neon badge tags; "
            f"nature: organic shape dividers, leaf clusters; retro: CRT bezels, pixel-art borders; "
            f"luxury: gold rule lines, large serif quotes),\n"
            f"Use data attributes like data-text, data-glitch, data-parallax on elements "
            f"so the regenerated CSS can target them for effects\n\n"
            f"LAYOUT — use inline styles for structure:\n"
            f"Use flexbox and CSS grid freely via inline style attributes "
            f"(display:flex, flex-direction, justify-content, align-items, flex-wrap, gap, "
            f"display:grid, grid-template-columns, grid-template-rows, place-items),\n"
            f"Create multi-column layouts, asymmetric grids, sticky sidebars, "
            f"full-width hero banners — whatever makes the theme look its best\n\n"
            f"STRUCTURE:\n"
            f"Completely restructure the HTML — new section order, new element types, "
            f"semantic tags (<section>, <article>, <header>, <nav>, <figure>, etc.),\n"
            f"Rewrite headings and microcopy to fit the theme's tone,\n"
            f"Preserve core factual content (product names, prices, key data)\n\n"
            f"IT IS VERY IMPORTANT THAT THE WEBSITE LOOKS CLEAN, IMPRESSIVE, AND NOT CLUNKY\n\n"
            f"RULES:\n"
             f"You MAY use inline styles for layout properties only "
            f"(flex, grid, position, width, height, gap, margin, padding for structural spacing),\n"
            f"Do NOT use inline styles for visual properties — colors, fonts, borders, shadows, "
            f"and backgrounds are all handled by the regenerated CSS,\n"
            f"Return ONLY valid HTML — no explanations, no markdown, no code fences,\n"
            f"Do not include <head>, <html>, or <body> wrapper tags"
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

    return response.choices[0].message.content


def regenerate_html(
    client: OpenAI,
    html: str,
    theme: str,
    on_chunk_complete: Optional[Callable[[int, int], None]] = None,
    base_url: str = "",
) -> str:
    """
    Regenerates HTML using GPT-4o, processing chunks in parallel.

    on_chunk_complete(chunk_index, total_chunks) is called (thread-safely by the
    caller's responsibility) after each chunk finishes — use it to publish
    per-chunk status updates from the handler.
    """
    chunks, labels, head_scripts = split_html_into_chunks(html, base_url=base_url)
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
    body_parts = [results[i] for i in range(1, len(chunks))]
    scripts_block = ("\n" + "\n".join(head_scripts)) if head_scripts else ""
    return (
        f"<!DOCTYPE html>\n<html>\n"
        f"<head>{head_content}{scripts_block}</head>\n"
        f"<body>{''.join(body_parts)}</body>\n"
        f"</html>"
    )
