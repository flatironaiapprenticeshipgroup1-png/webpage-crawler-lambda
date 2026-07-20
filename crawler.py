import os
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/ms-playwright"

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import requests

DEFAULT_MAX_CSS_FILE_CHARS = 500_000


def _get_max_css_file_chars() -> int:
    return int(os.environ.get("MAX_CSS_FILE_CHARS", DEFAULT_MAX_CSS_FILE_CHARS))


def crawl_website_html(url: str):
    print("Crawling the website...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process", "--no-zygote"]
        )
        page = browser.new_page()
        page.goto(url, timeout=600000)
        page.wait_for_load_state("domcontentloaded")
        html = page.content()
        browser.close()
    print(f"Successfully crawled HTML ({len(html)} bytes)")
    return html


def extract_css(html: str, base_url: str) -> list[dict[str, str]]:
    print("Parsing HTML to extract CSS references")
    soup = BeautifulSoup(html, "html.parser")
    sources = []

    for tag in soup.find_all(["link", "style"]):
        if tag.name == "style":
            css = tag.string
            if css:
                sources.append({"kind": "embedded", "css": str(css)})
            continue

        rel = tag.get("rel", [])
        if isinstance(rel, str):
            rel = rel.split()
        if tag.get("href") and any(token.lower() == "stylesheet" for token in rel):
            sources.append({"kind": "external", "url": urljoin(base_url, tag["href"])})

    return sources


def download_css_files(css_sources: list[dict[str, str]]) -> str:
    external_count = sum(source["kind"] == "external" for source in css_sources)
    print(f"Downloading {external_count} external CSS files")
    max_css_file_chars = _get_max_css_file_chars()
    combined = []
    for source in css_sources:
        if source["kind"] == "embedded":
            combined.append(source["css"])
            continue

        link = source["url"]
        try:
            response = requests.get(link, timeout=10)
            response.raise_for_status()
        except Exception as e:
            print(f"Warning: could not download CSS from {link}: {e}")
            continue

        if len(response.text) > max_css_file_chars:
            raise ValueError(
                f"CSS file {link} exceeds maximum allowed size of "
                f"{max_css_file_chars} characters (got {len(response.text)})"
            )

        combined.append(response.text)
        print(f"Downloaded CSS from {link} ({len(response.text)} bytes)")
    return "\n".join(combined)
