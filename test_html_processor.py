from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from html_processor import inline_svg_styles


def parse(html):
    return BeautifulSoup(html, "html.parser")


def test_document_without_svg_is_returned_unchanged():
    html = '<!DOCTYPE html><html><head><link rel="stylesheet" href="/a.css"></head>' \
           '<body><div class="card">Text</div></body></html>'
    assert inline_svg_styles(html, ".card { color: red; }") == html


def test_svg_class_is_removed_and_style_is_inlined():
    html = '<html><body><svg class="icon"><path class="icon-path" d="M0 0"/></svg></body></html>'
    css = ".icon { fill: blue; } .icon-path { stroke: green; }"

    soup = parse(inline_svg_styles(html, css))
    svg = soup.find("svg")
    path = soup.find("path")

    assert not svg.has_attr("class")
    assert not path.has_attr("class")
    assert "fill:blue" in svg["style"].replace(" ", "")
    assert "stroke:green" in path["style"].replace(" ", "")


def test_non_svg_document_content_is_left_untouched():
    html = (
        '<!DOCTYPE html><html><head><style>.old{color:red}</style>'
        '<link rel="stylesheet" href="/site.css"></head>'
        '<body><div class="card old">Card</div>'
        '<svg class="icon"><path/></svg></body></html>'
    )
    css = ".card { color: red; } .icon { fill: blue; }"

    soup = parse(inline_svg_styles(html, css))

    assert soup.head.find("style") is not None
    assert soup.head.find("link", rel="stylesheet") is not None
    card = soup.find("div", class_="card")
    assert card["class"] == ["card", "old"]
    assert not card.has_attr("style")
    assert result_doctype_preserved(html, str(soup))


def result_doctype_preserved(original, result):
    return ("<!DOCTYPE" in original) == ("<!DOCTYPE" in result.upper())


def test_class_shared_between_svg_and_non_svg_element_is_only_removed_from_svg():
    html = (
        '<html><body><div class="icon">Not an svg</div>'
        '<svg class="icon"><path/></svg></body></html>'
    )
    css = ".icon { fill: blue; color: red; }"

    soup = parse(inline_svg_styles(html, css))

    div = soup.find("div")
    svg = soup.find("svg")
    assert div["class"] == ["icon"]
    assert not svg.has_attr("class")


def test_multiple_svgs_are_each_inlined_independently():
    html = (
        '<html><body>'
        '<svg class="a"><path/></svg>'
        '<svg class="b"><path/></svg>'
        '</body></html>'
    )
    css = ".a { fill: red; } .b { fill: green; }"

    svgs = parse(inline_svg_styles(html, css)).find_all("svg")

    assert len(svgs) == 2
    assert "fill:red" in svgs[0]["style"].replace(" ", "")
    assert "fill:green" in svgs[1]["style"].replace(" ", "")
    assert not svgs[0].has_attr("class")
    assert not svgs[1].has_attr("class")


def test_existing_inline_style_wins_over_non_important_stylesheet_rule():
    html = '<html><body><svg class="icon" style="fill: purple;"><path/></svg></body></html>'
    css = ".icon { fill: blue; stroke: black; }"

    svg = parse(inline_svg_styles(html, css)).find("svg")
    style = svg["style"].replace(" ", "")

    assert "fill:purple" in style
    assert "stroke:black" in style


def test_important_stylesheet_rule_wins_over_existing_inline_style():
    html = '<html><body><svg class="icon" style="fill: purple;"><path/></svg></body></html>'
    css = ".icon { fill: blue !important; }"

    svg = parse(inline_svg_styles(html, css)).find("svg")
    style = svg["style"].lower().replace(" ", "")

    assert "fill:blue!important" in style


def test_premailer_failure_is_propagated():
    html = '<html><body><svg class="icon"><path/></svg></body></html>'
    with patch("html_processor.transform", side_effect=RuntimeError("premailer failed")):
        with pytest.raises(RuntimeError, match="premailer failed"):
            inline_svg_styles(html, ".icon { fill: blue; }")


def test_multiple_svgs_only_invoke_premailer_once():
    from html_processor import transform as real_transform

    html = (
        '<html><body>'
        '<svg class="a"><path/></svg>'
        '<svg class="b"><path/></svg>'
        '<svg class="c"><path/></svg>'
        '</body></html>'
    )
    css = ".a { fill: red; } .b { fill: green; } .c { fill: blue; }"

    with patch("html_processor.transform", wraps=real_transform) as mocked_transform:
        result = inline_svg_styles(html, css)

    assert mocked_transform.call_count == 1
    svgs = parse(result).find_all("svg")
    assert len(svgs) == 3
    assert "fill:red" in svgs[0]["style"].replace(" ", "")
    assert "fill:green" in svgs[1]["style"].replace(" ", "")
    assert "fill:blue" in svgs[2]["style"].replace(" ", "")


def test_merge_preserves_modern_css_syntax_cssutils_cannot_parse():
    html = (
        '<html><body><svg class="icon" '
        'style="color: hsl(var(--h), calc(var(--s) * 1%), 50%); border: 1px solid #0000;">'
        '<path/></svg></body></html>'
    )
    css = ".icon { fill: blue !important; }"

    svg = parse(inline_svg_styles(html, css)).find("svg")
    style = svg["style"]

    assert "hsl(var(--h), calc(var(--s) * 1%), 50%)" in style
    assert "1px solid #0000" in style
    assert "fill:blue!important" in style.replace(" ", "").lower()
