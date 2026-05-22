import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from urllib.parse import urljoin


def crawl_website_html(url: str) -> str:
    """
    Crawl a website and return its HTML content using Playwright.

    Args:
        url: The URL of the website to crawl

    Returns:
        str: The HTML content of the website
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url)
        html = page.content()
        browser.close()
    return html


def extract_css(html: str, base_url: str) -> dict:
    """
    Extract CSS links and inline styles from HTML content.

    Args:
        html: The HTML content to parse
        base_url: The base URL for resolving relative CSS links

    Returns:
        dict: A dictionary containing:
            - css_links: List of absolute URLs to external CSS files
            - inline_styles: List of inline CSS style content
    """
    soup = BeautifulSoup(html, "html.parser")
    css_links = [
        urljoin(base_url, tag["href"])
        for tag in soup.find_all("link", rel="stylesheet")
        if tag.get("href")
    ]
    inline_styles = [tag.string for tag in soup.find_all("style") if tag.string]
    return {"css_links": css_links, "inline_styles": inline_styles}


def download_css_files(css_links: list) -> str:
    """
    Download CSS files from a list of URLs and combine them.

    Args:
        css_links: List of CSS file URLs to download

    Returns:
        str: Combined CSS content from all downloaded files
    """
    combined = ""
    for link in css_links:
        try:
            response = requests.get(link, timeout=10)
            response.raise_for_status()
            combined += response.text + "\n"
        except Exception as e:
            print(f"Warning: could not download CSS from {link}: {e}")
    return combined


def crawl_website_and_generate_files(url: str) -> dict:
    """
    Complete workflow to crawl a website and extract all HTML and CSS content.

    This function orchestrates the crawling process by:
    1. Fetching the website HTML using Playwright
    2. Extracting CSS references and inline styles
    3. Downloading external CSS files
    4. Combining all CSS content

    Args:
        url: The URL of the website to crawl

    Returns:
        dict: A dictionary containing:
            - html: The full HTML content of the website
            - css: Combined CSS from external files and inline styles
    """
    html = crawl_website_html(url)
    css_info = extract_css(html, url)
    external_css = download_css_files(css_info["css_links"])
    all_css = external_css + "\n".join(css_info["inline_styles"])
    return {"html": html, "css": all_css}
