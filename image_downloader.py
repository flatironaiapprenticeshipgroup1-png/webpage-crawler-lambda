from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

MAX_IMAGE_BYTES = 5_000_000
_DOWNLOAD_WORKERS = 8


def extract_image_urls(html: str, base_url: str) -> list[str]:
    print("Parsing HTML to extract image references")
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    seen = set()
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src or src.startswith("data:"):
            continue
        absolute = urljoin(base_url, src)
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
    return urls


def _download_one(url: str):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        content = response.content
        if len(content) > MAX_IMAGE_BYTES:
            print(f"Warning: skipping image from {url} ({len(content)} bytes exceeds limit)")
            return None
        content_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        print(f"Downloaded image from {url} ({len(content)} bytes)")
        return url, content, content_type
    except Exception as e:
        print(f"Warning: could not download image from {url}: {e}")
        return None


def download_images(image_urls: list[str]) -> dict:
    """
    Downloads each image URL and returns {url: {"content": bytes, "content_type": str}}.
    URLs that fail to download (timeout, 4xx/5xx, oversized) are omitted from the result.
    """
    print(f"Downloading {len(image_urls)} image(s)")
    results = {}
    with ThreadPoolExecutor(max_workers=_DOWNLOAD_WORKERS) as executor:
        for result in executor.map(_download_one, image_urls):
            if result:
                url, content, content_type = result
                results[url] = {"content": content, "content_type": content_type}
    return results
