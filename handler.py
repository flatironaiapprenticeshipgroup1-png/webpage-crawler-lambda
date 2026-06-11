import json
import logging
import os
import threading
import uuid

import boto3
from openai import OpenAI

import html_regenerator
from crawler import crawl_website_html, extract_css, download_css_files

from status_publisher import get_current_sequence, publish_status_update


logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

secrets_client = boto3.client("secretsmanager")
s3 = boto3.client("s3")
dynamodb = boto3.client("dynamodb")
sqs = boto3.client("sqs")


def lambda_handler(event, context):
    failed_message_ids = []

    for record in event["Records"]:
        body = json.loads(record["body"])
        website_id = body["RegeneratedWebsiteId"]
        url = body["RegeneratedWebsiteUrl"]
        theme = body.get("RegenerationTheme", "")

        seq = get_current_sequence(website_id, url)
        seq_lock = threading.Lock()

        def publish(step, status, message, result_url=None, error=None):
            nonlocal seq
            with seq_lock:
                seq += 1
                current_seq = seq
            publish_status_update(
                website_id=website_id,
                website_url=url,
                phase="crawler",
                step=step,
                status=status,
                message=message,
                sequence=current_seq,
                result_url=result_url,
                error=error,
            )

        try:
            bucket = os.environ["S3_BUCKET_NAME"]
            table = os.environ["DYNAMODB_TABLE_NAME"]
            queue_url = os.environ["SQS_QUEUE_URL"]

            secret = json.loads(
                secrets_client.get_secret_value(SecretId=os.environ["SECRET_NAME"])["SecretString"]
            )
            openai_client = OpenAI(api_key=secret["OpenAIAPIKey"])

            print(f"Processing job {website_id} for {url}")
            publish("received", "processing", "Regeneration request received")

            print("Crawling HTML")
            publish("crawling_html", "processing", "Crawling the source website HTML")
            html = crawl_website_html(url)

            print("Extracting CSS")
            publish("extracting_css", "processing", "Extracting CSS references")
            css_info = extract_css(html, url)
            external_css = download_css_files(css_info["css_links"])
            all_css = external_css + "\n".join(css_info["inline_styles"])
            if not all_css.strip():
                all_css = "/* No source CSS found — generate complete theme stylesheet from scratch */\nbody {}\n"

            print("Creating HTML and CSS files")
            s3.put_object(Bucket=bucket, Key=f"{website_id}/index.html", Body=html, ContentType="text/html")
            s3.put_object(Bucket=bucket, Key=f"{website_id}/original-styles.css", Body=all_css, ContentType="text/css")
            dynamodb.put_item(
                TableName=table,
                Item={
                    "RegeneratedWebsiteId": {"S": website_id},
                    "RegeneratedWebsiteUrl": {"S": url},
                    "RegenerationTheme": {"S": theme},
                },
            )

            print("Regenerating HTML")
            publish("regenerating_html", "processing", "AI regenerating HTML structure")

            def on_chunk_complete(chunk_index, total_chunks):
                publish(
                    "regenerating_html_chunks_completed",
                    "processing",
                    f"Regenerated HTML chunk {chunk_index + 1} of {total_chunks}",
                )

            regenerated_html = html_regenerator.regenerate_html(openai_client, html, theme, on_chunk_complete, base_url=url)

            s3.put_object(
                Bucket=bucket,
                Key=f"{website_id}/Regenerated-Index.html",
                Body=regenerated_html.encode("utf-8"),
                ContentType="text/html",
                CacheControl="no-store, no-cache, must-revalidate",
            )
            logger.info("Regenerated HTML saved to S3 for website ID %s", website_id)

            print("Queuing AI regeneration step")
            publish("queueing_ai", "processing", "Queuing AI regeneration")
            sqs.send_message(
                QueueUrl=queue_url,
                MessageGroupId=str(uuid.uuid4()),
                MessageDeduplicationId=website_id,
                MessageBody=json.dumps({
                    "RegeneratedWebsiteId": website_id,
                    "RegeneratedWebsiteUrl": url,
                    "RegenerationTheme": theme,
                }),
            )

        except Exception as e:
            print(f"Error processing record {record['messageId']}: {e}")
            publish("failed", "failed", f"Crawler failed: {e}", error=str(e))
            failed_message_ids.append({"itemIdentifier": record["messageId"]})

    return {"batchItemFailures": failed_message_ids}
