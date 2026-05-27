import os
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/ms-playwright"

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import requests



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


def extract_css(html: str, base_url: str) -> dict:
    print("Parsing HTML to extract CSS references")
    soup = BeautifulSoup(html, "html.parser")
    css_links = [urljoin(base_url, link["href"]) for link in soup.find_all("link", rel="stylesheet") if link.get("href")]
    inline_styles = [style.get_text() for style in soup.find_all("style")]

    return {"css_links": css_links, "inline_styles": inline_styles}



def rewrite_html_for_regenerated_styles(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("link", rel="stylesheet"):
        tag.decompose()
    for tag in soup.find_all("style"):
        tag.decompose()
    head = soup.find("head")
    if head is None:
        head = soup.new_tag("head")
        soup.insert(0, head)
    head.insert(0, soup.new_tag("link", rel="stylesheet", href="Regenerated-Styles.css"))
    return str(soup)


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
