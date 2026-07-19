"""Prepare crawled HTML for downstream CSS regeneration."""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from cssutils.css import CSSStyleDeclaration
from premailer import transform


_HEAD_LINK_RELS_TO_REMOVE = {
    "preload",
    "prefetch",
    "dns-prefetch",
    "preconnect",
    "canonical",
    "alternate",
    "manifest",
    "stylesheet",
}
_HTML_DOCTYPE_RE = re.compile(r"<!doctype\s+html(?:\s+[^>]*)?>", re.IGNORECASE)
_ANY_DOCTYPE_RE = re.compile(r"<!doctype[^>]*>", re.IGNORECASE)


def _is_stylesheet_link(tag) -> bool:
    rel = tag.get("rel", [])
    if isinstance(rel, str):
        rel = rel.split()
    return any(token.lower() == "stylesheet" for token in rel)


def _rewrite_images(soup: BeautifulSoup, base_url: str, image_map: dict[str, str]) -> None:
    for image in soup.find_all("img"):
        src = image.get("src", "")
        if not src or src.startswith("data:"):
            continue
        absolute_url = urljoin(base_url, src)
        image["src"] = image_map.get(absolute_url, absolute_url)


def _clean_head(soup: BeautifulSoup) -> None:
    head = soup.find("head")
    if head is None:
        return

    for tag in head.find_all(["style", "noscript"]):
        tag.decompose()

    for tag in head.find_all("link"):
        rel = tag.get("rel", [])
        if isinstance(rel, str):
            rel = rel.split()
        normalized_rel = " ".join(rel).lower()
        if normalized_rel in _HEAD_LINK_RELS_TO_REMOVE:
            tag.decompose()

    for tag in head.find_all("meta"):
        property_value = tag.get("property", "")
        name = tag.get("name", "").lower()
        http_equiv = tag.get("http-equiv", "")
        if (
            property_value.startswith("og:")
            or name.startswith("twitter:")
            or http_equiv
            or name == "robots"
        ):
            tag.decompose()


def _mark_preserved_body_styles(soup: BeautifulSoup) -> None:
    """Keep body-level CSS elements outside Premailer's cleanup boundary."""
    for tag in soup.find_all(["style", "link"]):
        if tag.find_parent("head") is not None:
            continue
        if tag.name == "style" or _is_stylesheet_link(tag):
            tag["data-premailer"] = "ignore"


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


def _merge_original_inline_style(generated_css: str, original_css: str) -> str:
    generated = CSSStyleDeclaration(cssText=generated_css, validating=False)
    original = CSSStyleDeclaration(cssText=original_css, validating=False)

    for declaration in original:
        generated_priority = generated.getPropertyPriority(declaration.name).lower()
        original_priority = declaration.priority.lower()
        if generated_priority == "important" and original_priority != "important":
            continue
        generated.setProperty(declaration.name, declaration.value, declaration.priority)
    return generated.cssText


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

    for tag in soup.find_all(["style", "link"], attrs={"data-premailer": "ignore"}):
        if tag.find_parent("head") is None:
            del tag["data-premailer"]
    return str(soup)


def _preserve_html_doctype(source_html: str, output_html: str) -> str:
    if not _HTML_DOCTYPE_RE.search(source_html):
        return output_html

    if _ANY_DOCTYPE_RE.search(output_html):
        return _ANY_DOCTYPE_RE.sub("<!DOCTYPE html>", output_html, count=1)
    return f"<!DOCTYPE html>\n{output_html}"


def prepare_inline_html(
    html: str,
    css: str,
    base_url: str,
    image_map: dict[str, str],
) -> str:
    """Clean the source head, rewrite image sources, and inline source CSS.

    Relative image URLs intentionally resolve against ``base_url`` rather than a
    document ``<base>`` element. The ``<base>`` element itself is preserved.
    Relative CSS assets are not rebased; the combined source CSS is retained as a
    separate artifact for downstream processing.
    """
    soup = BeautifulSoup(html, "html.parser")
    _rewrite_images(soup, base_url, image_map)
    _clean_head(soup)
    _mark_preserved_body_styles(soup)
    inline_attribute, original_styles = _detach_inline_styles(soup)

    output = transform(
        str(soup),
        css_text=css,
        allow_network=False,
        disable_link_rewrites=True,
        remove_classes=False,
        keep_style_tags=False,
        disable_leftover_css=True,
        disable_validation=True,
        strip_important=False,
        align_floating_images=False,
        disable_basic_attributes=[
            "background",
            "bgcolor",
            "width",
            "height",
            "align",
            "valign",
        ],
    )

    output = _restore_inline_styles_and_remove_markers(
        output,
        inline_attribute,
        original_styles,
    )
    return _preserve_html_doctype(html, output)
