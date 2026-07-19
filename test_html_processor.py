from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from crawler import download_css_files, extract_css
from html_processor import prepare_inline_html


def parse(html):
    return BeautifulSoup(html, "html.parser")


def test_cleanup_is_limited_to_head_and_preserves_useful_content():
    removable_links = "".join(
        f'<link rel="{rel}" href="/{rel}.dat">'
        for rel in (
            "preload",
            "prefetch",
            "dns-prefetch",
            "preconnect",
            "canonical",
            "alternate",
            "manifest",
            "stylesheet",
        )
    )
    html = f"""<!DOCTYPE html>
    <html><head>
      <script id="head-script">window.ready = true;</script>
      <style>.removed {{ color: red; }}</style>
      <noscript id="head-noscript">removed</noscript>
      {removable_links}
      <link rel="icon" href="/favicon.ico">
      <link rel="alternate stylesheet" href="/optional.css">
      <meta property="og:title" content="remove">
      <meta name="TWITTER:card" content="remove">
      <meta http-equiv="refresh" content="5">
      <meta http-equiv="" content="keep">
      <meta name="ROBOTS" content="noindex">
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width">
      <meta name="description" content="keep">
      <meta name="keywords" content="keep">
      <title>Keep title</title>
    </head><body>
      <style id="body-style">.body-rule {{ color: blue; }}</style>
      <link id="body-link" rel="stylesheet" href="/body.css">
      <noscript id="body-noscript">keep body fallback</noscript>
      <p>Body</p>
    </body></html>"""

    result = prepare_inline_html(html, "", "https://example.com/page", {})
    soup = parse(result)
    head = soup.head

    assert head.find("script", id="head-script") is not None
    assert head.find("style") is None
    assert head.find("noscript") is None
    head_rel_values = [" ".join(link.get("rel", [])).lower() for link in head.find_all("link")]
    for rel in (
        "preload",
        "prefetch",
        "dns-prefetch",
        "preconnect",
        "canonical",
        "alternate",
        "manifest",
        "stylesheet",
    ):
        assert rel not in head_rel_values
    assert head.find("link", rel="icon") is not None
    assert head.find("link", rel=["alternate", "stylesheet"]) is not None
    assert head.find("meta", property="og:title") is None
    assert head.find("meta", attrs={"name": "TWITTER:card"}) is None
    assert head.find("meta", attrs={"http-equiv": "refresh"}) is None
    assert head.find("meta", attrs={"http-equiv": ""}) is not None
    assert head.find("meta", attrs={"name": "ROBOTS"}) is None
    assert head.find("meta", charset=True) is not None
    assert head.find("meta", attrs={"name": "viewport"}) is not None
    assert head.find("meta", attrs={"name": "description"}) is not None
    assert head.find("meta", attrs={"name": "keywords"}) is not None
    assert head.title.string == "Keep title"
    assert soup.find("style", id="body-style") is not None
    assert soup.find("link", id="body-link") is not None
    assert soup.find("noscript", id="body-noscript") is not None
    assert "data-premailer" not in result


def test_css_is_inlined_without_email_presentation_attributes():
    html = """<!DOCTYPE html><html><head>
      <style>.old { color: black; }</style>
      <link rel="stylesheet" href="/old.css">
    </head><body>
      <div id="hero" class="card old" style="color: green; font-size: 12px; padding: 1px">Hero</div>
    </body></html>"""
    css = """
      .card { color: red !important; font-size: 20px; background-color: blue; width: 10px; height: 20px; }
      #hero { float: left; text-align: center; }
    """

    soup = parse(prepare_inline_html(html, css, "https://example.com", {}))
    hero = soup.find(id="hero")
    style = hero["style"].lower()

    assert "color:red!important" in style.replace(" ", "")
    assert "font-size:12px" in style.replace(" ", "")
    assert "padding: 1px" in style
    assert "background-color: blue" in style
    assert hero["class"] == ["card", "old"]
    assert hero["id"] == "hero"
    assert soup.head.find("style") is None
    assert soup.head.find("link", rel="stylesheet") is None
    for attribute in ("bgcolor", "align", "valign", "width", "height"):
        assert not hero.has_attr(attribute)


@pytest.mark.parametrize(
    ("markup", "expected_sources", "winning_color"),
    [
        (
            '<style>.target { color: red; }</style><link rel="stylesheet" href="/site.css">',
            ["embedded", "external"],
            "blue",
        ),
        (
            '<link rel="stylesheet" href="/site.css"><style>.target { color: red; }</style>',
            ["external", "embedded"],
            "red",
        ),
    ],
)
def test_css_source_order_controls_the_inline_cascade(markup, expected_sources, winning_color):
    html = f"<!DOCTYPE html><html><head>{markup}</head><body><p class='target'>Text</p></body></html>"
    sources = extract_css(html, "https://example.com/page")
    response = MagicMock(text=".target { color: blue; }")
    response.raise_for_status.return_value = None

    with patch("crawler.requests.get", return_value=response):
        combined = download_css_files(sources)

    assert [source["kind"] for source in sources] == expected_sources
    first_color = "red" if expected_sources[0] == "embedded" else "blue"
    second_color = "blue" if first_color == "red" else "red"
    assert combined.index(f"color: {first_color}") < combined.index(f"color: {second_color}")
    target = parse(prepare_inline_html(html, combined, "https://example.com/page", {})).find(
        class_="target"
    )
    assert f"color:{winning_color}" in target["style"].replace(" ", "")


def test_body_style_is_preserved_but_marked_ignored_during_transform():
    html = """<html><body>
      <style id="body-style">.target { color: purple; }</style>
      <p class="target">Text</p>
    </body></html>"""
    css = ".target { color: purple; }"

    from html_processor import transform as real_transform

    with patch("html_processor.transform", wraps=real_transform) as mocked_transform:
        result = prepare_inline_html(html, css, "https://example.com", {})

    premailer_input = mocked_transform.call_args.args[0]
    assert 'data-premailer="ignore"' in premailer_input
    soup = parse(result)
    assert len(soup.find_all("style", id="body-style")) == 1
    assert "color:purple" in soup.find(class_="target")["style"].replace(" ", "")
    assert "data-premailer" not in result


def test_images_are_rewritten_and_base_element_is_intentionally_preserved():
    html = """<html><head><base href="https://cdn.example/assets/"></head><body>
      <img id="relative-cached" src="images/a.png">
      <img id="absolute-cached" src="https://images.example/b.jpg">
      <img id="uncached" src="images/c.png">
      <img id="data" src="data:image/png;base64,abc">
    </body></html>"""
    image_map = {
        "https://example.com/path/images/a.png": "./images/img-0.png",
        "https://images.example/b.jpg": "./images/img-1.jpg",
    }

    soup = parse(
        prepare_inline_html(html, "", "https://example.com/path/page.html", image_map)
    )

    assert soup.find("base")["href"] == "https://cdn.example/assets/"
    assert soup.find(id="relative-cached")["src"] == "./images/img-0.png"
    assert soup.find(id="absolute-cached")["src"] == "./images/img-1.jpg"
    assert soup.find(id="uncached")["src"] == "https://example.com/path/images/c.png"
    assert soup.find(id="data")["src"] == "data:image/png;base64,abc"


def test_body_content_and_attributes_survive_processing():
    html = """<html><body class="page" data-mode="source">
      Intro text <section id="content" class="panel" data-value="7">Section</section>
      <script id="body-script">window.bodyScript = true;</script>
      <style id="body-style">.panel { color: teal; }</style>
      <link id="body-link" rel="stylesheet" href="/body.css">
      <noscript id="body-noscript">Fallback</noscript>
    </body></html>"""

    soup = parse(prepare_inline_html(html, ".panel { color: teal; }", "https://example.com", {}))

    assert soup.body["class"] == ["page"]
    assert soup.body["data-mode"] == "source"
    assert "Intro text" in soup.body.get_text()
    section = soup.find("section", id="content")
    assert section["class"] == ["panel"]
    assert section["data-value"] == "7"
    assert soup.find("script", id="body-script") is not None
    assert soup.find("style", id="body-style") is not None
    assert soup.find("link", id="body-link") is not None
    assert soup.find("noscript", id="body-noscript") is not None


def test_missing_structure_preserves_meaningful_content_and_html_doctype():
    fragment = prepare_inline_html("<main>Fragment content</main>", "", "https://example.com", {})
    assert "Fragment content" in parse(fragment).get_text()

    standards = prepare_inline_html(
        "<!DoCtYpE html><main>Standards content</main>",
        "",
        "https://example.com",
        {},
    )
    assert standards.lstrip().startswith("<!DOCTYPE html>")
    assert "Standards content" in parse(standards).get_text()


def test_premailer_failure_is_propagated():
    with patch("html_processor.transform", side_effect=RuntimeError("premailer failed")):
        with pytest.raises(RuntimeError, match="premailer failed"):
            prepare_inline_html("<p>raw</p>", "p {}", "https://example.com", {})


def test_representative_modern_fixture_preserves_the_document_contract():
    html = """<!DoCtYpE html><html><head>
      <base href="https://cdn.example/assets/">
      <script id="head-script">window.ready = true;</script>
      <meta property="og:title" content="remove">
      <style>.target { color: red; }</style>
      <link rel="stylesheet" href="/external.css">
      <style>.target { color: green; }</style>
    </head><body>
      <div class="target" style="padding: 2px">Target</div>
      <img id="relative" src="img/photo.png">
      <img id="absolute" src="https://images.example/photo.jpg">
      <style id="body-modern">
        @media (max-width: 600px) { .target { display: grid; } }
        .target:hover { opacity: .5; }
        .target::before { content: "x"; }
        @keyframes fade { from { opacity: 0; } to { opacity: 1; } }
      </style>
      <link id="body-link" rel="stylesheet" href="/body.css">
      <noscript id="body-noscript">Fallback</noscript>
    </body></html>"""
    sources = extract_css(html, "https://example.com/path/page.html")

    def response_for(url, timeout):
        css = {
            "https://example.com/external.css": """
              @import url('./nested.css');
              @font-face { font-family: Demo; src: url('../fonts/demo.woff2'); }
              .target { color: blue; background-image: url('../img/bg.png'); }
            """,
            "https://example.com/body.css": ".target { border-color: black; }",
        }[url]
        response = MagicMock(text=css)
        response.raise_for_status.return_value = None
        return response

    with patch("crawler.requests.get", side_effect=response_for):
        combined = download_css_files(sources)

    result = prepare_inline_html(
        html,
        combined,
        "https://example.com/path/page.html",
        {
            "https://example.com/path/img/photo.png": "./images/img-0.png",
            "https://images.example/photo.jpg": "./images/img-1.jpg",
        },
    )
    soup = parse(result)
    target_style = soup.find(class_="target")["style"].replace(" ", "")

    assert [source["kind"] for source in sources] == [
        "embedded",
        "external",
        "embedded",
        "embedded",
        "external",
    ]
    assert "@import" in combined
    assert "@font-face" in combined
    assert "@media" in combined
    assert "@keyframes" in combined
    assert "../img/bg.png" in combined
    assert result.lstrip().startswith("<!DOCTYPE html>")
    assert soup.find("meta", property="og:title") is None
    assert soup.find("script", id="head-script") is not None
    assert soup.head.find("style") is None
    assert "color:green" in target_style
    assert "padding:2px" in target_style
    assert "background-image" in target_style
    assert "../img/bg.png" in target_style
    assert soup.find("style", id="body-modern") is not None
    assert soup.find("link", id="body-link") is not None
    assert soup.find("noscript", id="body-noscript") is not None
    assert soup.find(id="relative")["src"] == "./images/img-0.png"
    assert soup.find(id="absolute")["src"] == "./images/img-1.jpg"
    assert soup.find("base")["href"] == "https://cdn.example/assets/"
    assert "data-premailer" not in result
