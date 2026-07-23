"""Inline CSS onto <svg> elements only, ahead of AI HTML regeneration.

Everything outside <svg> subtrees keeps its class-based styling untouched —
the AI HTML regenerator (html_regenerator.py) preserves those classes and
the AI CSS lambda regenerates the stylesheet that targets them. SVGs are the
one case inlined here because selector-based CSS rewrites tend to mangle svg
styling; their classes are stripped and their computed style is baked
in as a style="..." attribute instead.
"""

from bs4 import BeautifulSoup
from cssutils.css import CSSStyleDeclaration
from premailer import transform


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

    Each <svg> is inlined in isolation (as its own fragment) so the rest of the
    document's classes, stylesheet links, and head content are left completely
    untouched — those stay class-based for the AI HTML/CSS regeneration passes.
    """
    soup = BeautifulSoup(html, "html.parser")
    svgs = soup.find_all("svg")
    if not svgs:
        return html

    for svg in svgs:
        svg_soup = BeautifulSoup(str(svg), "html.parser")
        inline_attribute, original_styles = _detach_inline_styles(svg_soup)

        inlined = transform(
            str(svg_soup),
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

        new_svg = BeautifulSoup(inlined, "html.parser").find("svg")
        if new_svg is not None:
            svg.replace_with(new_svg)

    return str(soup)
