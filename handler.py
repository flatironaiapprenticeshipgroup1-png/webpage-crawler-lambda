import json
import os
import uuid
import boto3
from crawler import crawl_website_html, extract_css, download_css_files
from status_publisher import publish_status_update

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

        seq = 0

        def publish(step, status, message, result_url=None, error=None):
            nonlocal seq
            seq += 1
            publish_status_update(
                website_id=website_id,
                website_url=url,
                phase="crawler",
                step=step,
                status=status,
                message=message,
                sequence=seq,
                result_url=result_url,
                error=error,
            )

        try:
            bucket = os.environ["S3_BUCKET_NAME"]
            table = os.environ["DYNAMODB_TABLE_NAME"]
            queue_url = os.environ["SQS_QUEUE_URL"]

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

            print("Creating HTML and CSS files")
            s3.put_object(
                Bucket=bucket,
                Key=f"{website_id}/index.html",
                Body=html,
                ContentType="text/html"
            )
            s3.put_object(
                Bucket=bucket,
                Key=f"{website_id}/original-styles.css",
                Body=all_css,
                ContentType="text/css"
            )
            dynamodb.put_item(
                TableName=table,
                Item={
                    "RegeneratedWebsiteId": {"S": website_id},
                    "RegeneratedWebsiteUrl": {"S": url},
                    "RegenerationTheme": {"S": theme},
                },
            )

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
