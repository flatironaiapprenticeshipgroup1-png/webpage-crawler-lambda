import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from urllib.parse import urljoin


def crawl_website_html(url: str) -> str:
    print(f"Launching Playwright browser to crawl {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        page = browser.new_page()
        page.goto(url)
        page.wait_for_load_state("networkidle")
        html = page.content()
        browser.close()
    print(f"Successfully crawled HTML ({len(html)} bytes)")
    return html


def extract_css(html: str, base_url: str) -> dict:
    print("Parsing HTML to extract CSS references")
    soup = BeautifulSoup(html, "html.parser")
    css_links = [
        urljoin(base_url, tag["href"])
        for tag in soup.find_all("link", rel="stylesheet")
        if tag.get("href")
    ]
    inline_styles = [tag.string for tag in soup.find_all("style") if tag.string]
    print(f"Found {len(css_links)} external CSS links and {len(inline_styles)} inline style blocks")
    return {"css_links": css_links, "inline_styles": inline_styles}


def download_css_files(css_links: list) -> str:
    print(f"Downloading {len(css_links)} external CSS files")
    combined = ""
    for link in css_links:
        try:
            response = requests.get(link, timeout=10)
            response.raise_for_status()
            combined += response.text + "\n"
            print(f"Downloaded CSS from {link} ({len(response.text)} bytes)")
        except Exception as e:
            print(f"Warning: could not download CSS from {link}: {e}")
    return combined


def crawl_website_and_generate_files(url: str) -> dict:
    print(f"Starting full crawl workflow for {url}")
    html = crawl_website_html(url)
    css_info = extract_css(html, url)
    external_css = download_css_files(css_info["css_links"])
    all_css = external_css + "\n".join(css_info["inline_styles"])
    print(f"Crawl complete: {len(html)} bytes HTML, {len(all_css)} bytes CSS")
    return {"html": html, "css": all_css}
