import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import openai
from openai import OpenAI
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

MAX_CHARS_PER_CHUNK = 30_000


def split_html_into_chunks(html: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> tuple[list[str], list[str]]:
    """
    Returns (chunks, labels) where labels are 'head' or 'body'.
    Chunk 0 is always the <head> element; subsequent chunks are groups
    of top-level <body> children packed to max_chars each.
    Falls back to a single raw chunk if the document can't be parsed.
    """
    soup = BeautifulSoup(html, "html.parser")
    head = soup.find("head")
    body = soup.find("body")

    if not head or not body:
        return [html], ["raw"]

    chunks = [str(head)]
    labels = ["head"]

    current_parts: list[str] = []
    current_size = 0

    for child in body.children:
        child_str = str(child)
        child_size = len(child_str)
        if current_parts and current_size + child_size > max_chars:
            chunks.append("".join(current_parts))
            labels.append("body")
            current_parts = [child_str]
            current_size = child_size
        else:
            current_parts.append(child_str)
            current_size += child_size

    if current_parts:
        chunks.append("".join(current_parts))
        labels.append("body")

    return chunks, labels


def _regenerate_chunk(
    client: OpenAI,
    chunk: str,
    label: str,
    theme: str,
    chunk_index: int,
    total_chunks: int,
) -> str:
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
) -> str:
    """
    Regenerates HTML using GPT-4o, processing chunks in parallel.

    on_chunk_complete(chunk_index, total_chunks) is called (thread-safely by the
    caller's responsibility) after each chunk finishes — use it to publish
    per-chunk status updates from the handler.
    """
    chunks, labels = split_html_into_chunks(html)
    logger.info("Split HTML into %d chunk(s) for processing", len(chunks))

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
    return (
        f"<!DOCTYPE html>\n<html>\n"
        f"<head>{head_content}</head>\n"
        f"<body>{''.join(body_parts)}</body>\n"
        f"</html>"
    )
