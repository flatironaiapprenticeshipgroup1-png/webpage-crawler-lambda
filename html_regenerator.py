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
            f"You are an HTML and web design expert specializing in dramatic visual transformations.\n\n"
            f"You will receive an HTML fragment. Completely rewrite it to match the theme: {theme}\n\n"
            f"You MUST change ALL of the following:\n\n"
            f"LAYOUT & STRUCTURE:\n"
            f"Redesign the layout entirely — change the arrangement of sections, use different HTML elements,\n"
            f"Add theme-appropriate structural elements (hero sections, cards, grids, overlays, etc.),\n"
            f"Restructure the hierarchy dramatically so it looks nothing like the original\n\n"
            f"SEMANTIC MARKUP:\n"
            f"Replace generic divs with semantic elements where appropriate (<section>, <article>, <header>, <nav>, etc.),\n"
            f"Add theme-appropriate attributes and ARIA labels\n\n"
            f"CLASS NAMES & IDs:\n"
            f"You MAY rewrite class names to be theme-appropriate,\n"
            f"Preserve any class names that are critical for functionality (e.g. nav toggles, modals)\n\n"
            f"CONTENT:\n"
            f"You MAY rewrite headings, labels, and microcopy to fit the theme's tone and vocabulary,\n"
            f"Preserve core factual content (product names, prices, data)\n\n"
            f"THEME ELEMENTS:\n"
            f"Add decorative HTML elements that match the theme\n"
            f"(e.g. cyberpunk: scanline overlays, glitch spans; nature: leaf/wave decorations; retro: CRT frames)\n\n"
            f"also add dramatic decorative elements in the body to make the website more visually engaging\n\n"
            f"IT IS VERY IMPORTANT THAT THE RESULT LOOKS COMPLETELY DIFFERENT FROM THE ORIGINAL\n\n"
            f"RULES:\n"
            f"Do NOT add inline style attributes — the CSS handles all visual styling,\n"
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
