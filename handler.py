import json
import boto3
from crawler import crawl_website_html, extract_css, download_css_files
from status_publisher import publish_status_update

s3 = boto3.client("s3")
dynamodb = boto3.client("dynamodb")
sqs = boto3.client("sqs")


def lambda_handler(event, context):
    """
    AWS Lambda handler for website crawling and AI regeneration workflow.

    Processes SQS messages containing website regeneration requests. For each request:
    1. Crawls the source website HTML
    2. Extracts and downloads CSS files
    3. Saves original HTML and CSS to S3
    4. Stores job metadata in DynamoDB
    5. Queues the AI regeneration step to SQS

    Publishes status updates at each step via Ably real-time channels.

    Args:
        event: SQS event with Records containing RegeneratedWebsiteId, RegeneratedWebsiteUrl, RegenerationTheme
        context: Lambda context object

    Returns:
        dict: Response with statusCode 200
    """
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
                phase="crawler",
                step=step,
                status=status,
                message=message,
                sequence=seq,
                result_url=result_url,
                error=error,
            )

        try:
            import os
            bucket = os.environ["S3_BUCKET_NAME"]
            table = os.environ["DYNAMODB_TABLE_NAME"]
            queue_url = os.environ["SQS_QUEUE_URL"]

            publish("received", "processing", "Regeneration request received")

            publish("crawling_html", "processing", "Crawling the source website HTML")
            html = crawl_website_html(url)

            publish("extracting_css", "processing", "Extracting CSS references")
            css_info = extract_css(html, url)
            external_css = download_css_files(css_info["css_links"])
            all_css = external_css + "\n".join(css_info["inline_styles"])

            publish("saving_original_assets", "processing", "Saving original HTML and CSS to S3")
            s3.put_object(Bucket=bucket, Key=f"{website_id}/index.html", Body=html, ContentType="text/html")
            s3.put_object(Bucket=bucket, Key=f"{website_id}/original-styles.css", Body=all_css, ContentType="text/css")

            publish("saving_metadata", "processing", "Saving job metadata to DynamoDB")
            dynamodb.put_item(
                TableName=table,
                Item={
                    "RegeneratedWebsiteId": {"S": website_id},
                    "RegeneratedWebsiteUrl": {"S": url},
                    "RegenerationTheme": {"S": theme},
                },
            )

            publish("queueing_ai", "processing", "Queuing AI regeneration step")
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps({
                    "RegeneratedWebsiteId": website_id,
                    "RegeneratedWebsiteUrl": url,
                    "RegenerationTheme": theme,
                }),
                MessageGroupId="website-regeneration",
                MessageDeduplicationId=website_id,
            )

        except Exception as e:
            publish("failed", "failed", f"Crawler failed: {e}", error=str(e))
            raise

    return {"statusCode": 200}
