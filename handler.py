import json
from typing import TypedDict, Optional
from crawler import crawl_website_and_generate_files
import boto3
import os


class RegenerateWebsiteEvent(TypedDict):
    RegeneratedWebsiteId: str
    RegeneratedWebsiteUrl: str
    RegenerationTheme: Optional[str]


bucket_name = os.environ.get("S3_BUCKET_NAME")
s3 = boto3.client("s3")
ddb = boto3.client("dynamodb")
sqs = boto3.client("sqs")


def lambda_handler(event, context):
    failed_message_ids = []
    for record in event["Records"]:
        try:
            body: RegenerateWebsiteEvent = json.loads(record["body"])
            files = crawl_website_and_generate_files(body["RegeneratedWebsiteUrl"])

            print("Creating HTML and CSS files")
            s3.put_object(
                Bucket=bucket_name,
                Key=f"{body['RegeneratedWebsiteId']}/index.html",
                Body=files["html"]
            )
            s3.put_object(
                Bucket=bucket_name,
                Key=f"{body['RegeneratedWebsiteId']}/original-styles.css",
                Body=files["css"]
            )
            ddb.put_item(
                TableName=os.environ.get("DYNAMODB_TABLE_NAME"),
                Item={
                    "RegeneratedWebsiteId": {"S": body["RegeneratedWebsiteId"]},
                    "RegeneratedWebsiteUrl": {"S": body["RegeneratedWebsiteUrl"]},
                    "RegenerationTheme": {"S": body.get("RegenerationTheme", "default")},
                }
            )
            sqs.send_message(
                QueueUrl=os.environ.get("SQS_QUEUE_URL"),
                MessageGroupId=body["RegeneratedWebsiteId"],
                MessageBody=json.dumps({
                    "RegeneratedWebsiteId": body["RegeneratedWebsiteId"],
                    "RegeneratedWebsiteUrl": body["RegeneratedWebsiteUrl"],
                    "RegenerationTheme": body.get("RegenerationTheme")
                })
            )
        except Exception as e:
            print(f"Error processing record {record['messageId']}: {e}")
            failed_message_ids.append({"itemIdentifier": record["messageId"]})

    return {"batchItemFailures": failed_message_ids}
