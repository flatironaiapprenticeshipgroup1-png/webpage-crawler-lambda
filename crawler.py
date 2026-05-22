from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import requests
import os


def crawl_website_and_generate_files(url: str):
    try:
        html = crawl_website_html(url)
        css_data = extract_css(html, url)
        all_css = ""
        for link in css_data["css_links"]:
            response = requests.get(link, timeout=10)
            response.raise_for_status()
            all_css += response.text
        all_css += "\n".join([style for style in css_data["inline_styles"] if style])
        return {"html": html, "css": all_css}
    except Exception as e:
        print(f"Error crawling website: {e}")
        raise


def crawl_website_html(url: str):
    print("Crawling the website...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        page = browser.new_page()
        page.goto(url)
        page.wait_for_load_state("networkidle")
        html = page.content()
        print("Crawled content length:", len(html))
        browser.close()
    return html

def extract_css(html: str, base_url: str):
    print("Extracting CSS from HTML...")
    soup = BeautifulSoup(html, "html.parser")
    css_links = [urljoin(base_url, link["href"]) for link in soup.find_all("link", rel="stylesheet")]
    inline_styles = [style.string for style in soup.find_all("style")]

    return {"css_links": css_links, "inline_styles": inline_styles}