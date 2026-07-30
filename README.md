# webpage-crawler-lambda

First stage of the AI Website Regenerator pipeline. Given a source URL and an
optional theme, this Lambda crawls the live site, captures its original
HTML/CSS/images, regenerates the HTML with GPT-4o, and hands off to
[`ai-css-regeneration-lambda`](../ai-css-regeneration-lambda) for CSS
regeneration.

## What it does

Triggered by an SQS message containing `{RegeneratedWebsiteId,
RegeneratedWebsiteUrl, RegenerationTheme}`, `handler.py` runs:

1. **Crawl** (`crawler.py`) — loads the target URL in headless Chromium via
   Playwright and captures the rendered HTML.
2. **Extract CSS** (`crawler.py`) — parses `<link rel="stylesheet">` and
   `<style>` tags out of the HTML and downloads any external stylesheets.
3. **Extract & cache images** (`image_downloader.py`) — finds image URLs
   referenced in the page and downloads them to S3 under
   `{website_id}/images/`, building a map from original URL to the new S3
   path.
4. **Inline SVG styles** (`html_processor.py`) — inlines relevant CSS onto
   `<svg>` elements so they still render correctly once external
   stylesheets are replaced later in the pipeline.
5. **Save originals to S3** — writes `{website_id}/index.html` and
   `{website_id}/original-styles.css`, and creates the job's DynamoDB
   record.
6. **Regenerate HTML** (`html_regenerator.py`) — splits the HTML into chunks
   (by walking the DOM with BeautifulSoup, packing body children up to
   ~30k characters per chunk), sends each chunk to GPT-4o in parallel to
   restructure/re-theme it while preserving class names, ids, and image
   `src` attributes (so the separately-regenerated CSS still targets the
   right selectors), then reassembles and writes
   `{website_id}/Regenerated-Index.html` to S3.
7. **Queue CSS regeneration** — sends a message to the downstream SQS queue
   so `ai-css-regeneration-lambda` can regenerate the CSS.

Progress is published at each step to Ably (channel
`regeneration:{website_id}`, event `regeneration-status`) and mirrored to
DynamoDB, so the frontend can show live status.

## Files

| File | Purpose |
|---|---|
| `handler.py` | Lambda entry point; orchestrates the steps above |
| `crawler.py` | Playwright-based page crawl + CSS source extraction/download |
| `html_regenerator.py` | Chunks HTML by DOM structure and drives GPT-4o regeneration |
| `html_processor.py` | Inlines CSS onto SVG elements |
| `image_downloader.py` | Finds and downloads page images |
| `status_publisher.py` | Publishes progress to Ably and persists status to DynamoDB |
| `test_handler.py`, `test_html_processor.py`, `test_html_regenerator.py` | Unit tests |

## Environment variables

- `S3_BUCKET_NAME`
- `DYNAMODB_TABLE_NAME`
- `SQS_QUEUE_URL` — downstream queue consumed by `ai-css-regeneration-lambda`
- `SECRET_NAME` — Secrets Manager secret containing `OpenAIAPIKey`
- `ABLY_SECRET_NAME` — Secrets Manager secret containing `AblyApiKey`
- `MAX_CSS_FILE_CHARS` (optional, default 500,000) — max size of a single
  downloaded external CSS file before the crawl fails

## Running locally / deployment

Packaged as a container image (see `Dockerfile`) built on
`public.ecr.aws/lambda/python:3.12`, since Playwright/Chromium need native
system libraries not available in the standard Lambda runtime. Dependencies
are listed in `requirements.txt`.

```bash
docker build -t webpage-crawler-lambda .
```

Tests:

```bash
pip install -r requirements.txt
pytest
```
