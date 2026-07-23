"""Inline CSS onto <svg> elements only, ahead of AI HTML regeneration.

Everything outside <svg> subtrees keeps its class-based styling untouched —
the AI HTML regenerator (html_regenerator.py) preserves those classes and
the AI CSS lambda regenerates the stylesheet that targets them. SVGs are the
one case inlined here because selector-based CSS rewrites tend to mangle svg
styling; their classes are stripped and their computed style is baked
in as a style="..." attribute instead.
"""

import logging
import re

import cssutils
from bs4 import BeautifulSoup
from premailer import transform

# cssutils has poor support for modern CSS (var(), calc(), min()/max(),
# hsl()/rgba() with var() arguments, 4-digit hex) and logs an ERROR for
# every declaration it can't parse — harmless (it degrades gracefully
# rather than raising) but extremely noisy against a real-world stylesheet.
# Premailer depends on cssutils internally for its own stylesheet parsing,
# so lowering the log level here (the same process-wide logger) is the only
# way to quiet it.
cssutils.log.setLevel(logging.CRITICAL)

_IMPORTANT_RE = re.compile(r"!\s*important\s*$", re.IGNORECASE)


def _split_declarations(css_text: str) -> list[str]:
    """Splits a `prop: value; prop2: value2;` string on top-level semicolons
    only — not ones inside a quoted string or a function call's parentheses —
    mirroring the depth-tracking approach ai-css-regeneration-lambda's
    parse_css_blocks uses for CSS rule blocks, just applied to a declaration
    list instead of a stylesheet."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote = None
    for ch in css_text:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        if ch == ";" and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _parse_declarations(css_text: str) -> dict[str, tuple[str, str]]:
    """Parses a declaration list into {property: (value, priority)}, in
    source order. priority is "important" or "". Deliberately never
    inspects a value's internal grammar — unlike cssutils' CSSStyleDeclaration,
    this lets var()/calc()/min()/max()/etc. pass through untouched instead of
    silently failing to parse and getting dropped."""
    declarations: dict[str, tuple[str, str]] = {}
    for raw in _split_declarations(css_text):
        name, sep, value = raw.partition(":")
        if not sep:
            continue
        name = name.strip().lower()
        value = value.strip()
        priority = ""
        if _IMPORTANT_RE.search(value):
            value = _IMPORTANT_RE.sub("", value).strip()
            priority = "important"
        if name:
            declarations[name] = (value, priority)
    return declarations


def _merge_original_inline_style(generated_css: str, original_css: str) -> str:
    generated = _parse_declarations(generated_css)
    original = _parse_declarations(original_css)

    for name, (value, priority) in original.items():
        existing = generated.get(name)
        if existing and existing[1] == "important" and priority != "important":
            continue
        generated[name] = (value, priority)

    return "; ".join(
        f"{name}: {value}" + (" !important" if priority == "important" else "")
        for name, (value, priority) in generated.items()
    )


def _detach_inline_styles(soup: BeautifulSoup) -> tuple[str, dict[str, str]]:
    """Temporarily remove inline styles so stylesheet ``!important`` can win.

    Premailer otherwise treats every existing inline declaration as important,
    which differs from the browser cascade when a stylesheet declaration is
    explicitly marked ``!important``.
    """
    attribute = "data-crawler-inline-cascade"
    suffix = 0
    while soup.find(attrs={attribute: True}) is not None:
        suffix += 1
        attribute = f"data-crawler-inline-cascade-{suffix}"

    original_styles = {}
    for index, tag in enumerate(soup.find_all(style=True)):
        marker = str(index)
        original_styles[marker] = tag["style"]
        tag[attribute] = marker
        del tag["style"]
    return attribute, original_styles


def _restore_inline_styles_and_remove_markers(
    html: str,
    inline_attribute: str,
    original_styles: dict[str, str],
) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(attrs={inline_attribute: True}):
        marker = tag[inline_attribute]
        merged = _merge_original_inline_style(tag.get("style", ""), original_styles[marker])
        if merged:
            tag["style"] = merged
        elif tag.has_attr("style"):
            del tag["style"]
        del tag[inline_attribute]
    return str(soup)


def inline_svg_styles(html: str, css: str) -> str:
    """Inline ``css`` onto every <svg> subtree in ``html`` and strip their classes.

    All <svg>s are inlined together in a single premailer pass (rather than
    one pass per svg) so a page with many small icon svgs doesn't re-parse
    the entire stylesheet once per svg. The rest of the document's classes,
    stylesheet links, and head content are left completely untouched — those
    stay class-based for the AI HTML/CSS regeneration passes.
    """
    soup = BeautifulSoup(html, "html.parser")
    svgs = soup.find_all("svg")
    if not svgs:
        return html

    combined_soup = BeautifulSoup(
        "".join(str(svg) for svg in svgs), "html.parser"
    )
    inline_attribute, original_styles = _detach_inline_styles(combined_soup)

    # Known limitation: premailer parses the stylesheet via cssutils, which
    # can't represent calc()/min()/max() or color functions (hsl()/rgba())
    # wrapping var()/math — those declarations are silently dropped rather
    # than inlined, even though the log-noise from attempting to parse them
    # is now silenced above. A plain `var(--x)` or `calc(10px + 5px)` inlines
    # fine; it's specifically those combinations that don't.
    inlined = transform(
        str(combined_soup),
        css_text=css,
        allow_network=False,
        disable_link_rewrites=True,
        remove_classes=True,
        keep_style_tags=False,
        disable_leftover_css=True,
        disable_validation=True,
        strip_important=False,
        align_floating_images=False,
    )
    inlined = _restore_inline_styles_and_remove_markers(
        inlined, inline_attribute, original_styles,
    )

    new_svgs = BeautifulSoup(inlined, "html.parser").find_all("svg")
    for old_svg, new_svg in zip(svgs, new_svgs):
        old_svg.replace_with(new_svg)

    return str(soup)
