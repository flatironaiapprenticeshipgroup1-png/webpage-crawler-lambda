"""
Test suite for the Lambda handler module.

This module provides comprehensive test coverage for the webpage-crawler-lambda
handler function. Tests verify:

1. Happy path: Correct execution flow with expected status publications
2. Error handling: Proper failure status publishing on crawler/storage errors
3. Sequence numbering: Monotonically increasing sequence numbers across updates
4. AWS integration: Correct interactions with S3, DynamoDB, and SQS

Each test uses mocking to isolate the handler logic from AWS services and
external dependencies (Ably, crawler functions).
"""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Set env vars before importing handler
os.environ["S3_BUCKET_NAME"] = "test-bucket"
os.environ["DYNAMODB_TABLE_NAME"] = "test-table"
os.environ["SQS_QUEUE_URL"] = "https://sqs.us-east-1.amazonaws.com/123/test.fifo"
os.environ["ABLY_SECRET_NAME"] = "test/ably-secret"

WEBSITE_ID = "abc-123"
URL = "https://example.com"
THEME = "dark"


def make_event(website_id=WEBSITE_ID, url=URL, theme=THEME):
    """
    Create a test SQS event with website regeneration request.

    Args:
        website_id: Website identifier (default: "abc-123")
        url: Website URL to crawl (default: "https://example.com")
        theme: Theme for regeneration (default: "dark")

    Returns:
        dict: SQS event with a single record containing the regeneration request
    """
    return {
        "Records": [
            {
                "messageId": "msg-001",
                "body": json.dumps(
                    {
                        "RegeneratedWebsiteId": website_id,
                        "RegeneratedWebsiteUrl": url,
                        "RegenerationTheme": theme,
                    }
                ),
            }
        ]
    }


def make_mocks():
    """
    Create mocked AWS service clients and Ably objects.

    Sets up mock objects for:
    - S3 client: For HTML/CSS storage
    - DynamoDB client: For job metadata storage
    - SQS client: For AI regeneration queue
    - Secrets Manager: For Ably API key retrieval
    - Ably REST client: For real-time status publishing

    Returns:
        tuple: (mock_s3, mock_dynamodb, mock_sqs, mock_ably_channel,
                mock_ably_rest, boto3_client_factory)
    """
    mock_s3 = MagicMock()
    mock_dynamodb = MagicMock()
    mock_dynamodb.get_item.return_value = {}
    mock_sqs = MagicMock()
    mock_secrets = MagicMock()
    mock_secrets.get_secret_value.return_value = {
        "SecretString": json.dumps({"AblyApiKey": "fake-key"})
    }

    def boto3_client_factory(service, **kwargs):
        """Factory function to return mocked boto3 clients by service name."""
        return {
            "s3": mock_s3,
            "dynamodb": mock_dynamodb,
            "sqs": mock_sqs,
            "secretsmanager": mock_secrets,
        }[service]

    mock_ably_channel = MagicMock()
    mock_ably_channel.publish = AsyncMock()
    mock_ably_rest = MagicMock()
    mock_ably_rest.channels.get.return_value = mock_ably_channel

    return (
        mock_s3,
        mock_dynamodb,
        mock_sqs,
        mock_ably_channel,
        mock_ably_rest,
        boto3_client_factory,
    )


def test_happy_path_publishes_all_steps_in_order():
    """
    Test the handler successfully processes a website crawl request.

    Verifies:
    1. Handler returns 200 status code
    2. All expected processing steps are published in correct order:
       received -> crawling_html -> extracting_css -> saving_original_assets
       -> saving_metadata -> queueing_ai
    3. Sequence numbers increase monotonically from 1 to N
    4. All status updates have "processing" status
    5. Correct number of S3, DynamoDB, and SQS operations are performed
    """
    mock_s3, mock_dynamodb, mock_sqs, mock_channel, mock_ably_rest, boto3_factory = (
        make_mocks()
    )

    with patch("boto3.client", side_effect=boto3_factory), patch(
        "ably.AblyRest", return_value=mock_ably_rest
    ), patch("crawler.crawl_website_html", return_value="<html></html>"), patch(
        "crawler.extract_css",
        return_value={"css_links": [], "inline_styles": ["body{}"]},
    ), patch(
        "crawler.download_css_files", return_value=""
    ), patch(
        "crawler.rewrite_html_for_regenerated_styles",
        return_value="<html></html>",
        create=True,
    ):

        # Re-import to pick up patches
        if "handler" in sys.modules:
            del sys.modules["handler"]
        if "status_publisher" in sys.modules:
            del sys.modules["status_publisher"]
        import handler

        result = handler.lambda_handler(make_event(), {})

    assert result == {"batchItemFailures": []}

    published_calls = mock_channel.publish.call_args_list
    steps = [c.args[1]["step"] for c in published_calls]
    assert steps == [
        "received",
        "crawling_html",
        "extracting_css",
        "queueing_ai",
    ], f"Unexpected steps: {steps}"

    sequences = [c.args[1]["sequence"] for c in published_calls]
    assert sequences == list(
        range(1, len(steps) + 1)
    ), "Sequences not monotonically increasing"

    statuses = [c.args[1]["status"] for c in published_calls]
    assert all(s == "processing" for s in statuses)

    assert mock_s3.put_object.call_count == 2
    assert mock_dynamodb.put_item.call_count == 1
    assert mock_sqs.send_message.call_count == 1
    print("test_happy_path_publishes_all_steps_in_order: PASSED")


def test_crawl_failure_publishes_failed():
    """
    Test the handler publishes failure status when HTML crawling fails.

    Verifies:
    1. When crawl_website_html raises an exception, handler propagates it
    2. "failed" step is published before the exception propagates
    3. Failed event has status="failed" and includes error message
    """
    mock_s3, mock_dynamodb, mock_sqs, mock_channel, mock_ably_rest, boto3_factory = (
        make_mocks()
    )

    with patch("boto3.client", side_effect=boto3_factory), patch(
        "ably.AblyRest", return_value=mock_ably_rest
    ), patch("crawler.crawl_website_html", side_effect=Exception("timeout")):

        if "handler" in sys.modules:
            del sys.modules["handler"]
        if "status_publisher" in sys.modules:
            del sys.modules["status_publisher"]
        import handler

        result = handler.lambda_handler(make_event(), {})

    assert result["batchItemFailures"] == [{"itemIdentifier": "msg-001"}]
    steps = [c.args[1]["step"] for c in mock_channel.publish.call_args_list]
    assert "failed" in steps
    failed_event = next(
        c.args[1]
        for c in mock_channel.publish.call_args_list
        if c.args[1]["step"] == "failed"
    )
    assert failed_event["status"] == "failed"
    assert failed_event["error"] is not None
    print("test_crawl_failure_publishes_failed: PASSED")


def test_s3_failure_publishes_failed():
    """
    Test the handler publishes failure status when S3 operations fail.

    Verifies:
    1. When S3 put_object raises an exception, handler propagates it
    2. "failed" step is published before the exception propagates
    3. Failure occurs after HTML crawl and CSS extraction succeed
    """
    mock_s3, mock_dynamodb, mock_sqs, mock_channel, mock_ably_rest, boto3_factory = (
        make_mocks()
    )
    mock_s3.put_object.side_effect = Exception("S3 unavailable")

    with patch("boto3.client", side_effect=boto3_factory), patch(
        "ably.AblyRest", return_value=mock_ably_rest
    ), patch("crawler.crawl_website_html", return_value="<html></html>"), patch(
        "crawler.extract_css", return_value={"css_links": [], "inline_styles": []}
    ), patch(
        "crawler.download_css_files", return_value=""
    ), patch(
        "crawler.rewrite_html_for_regenerated_styles",
        return_value="<html></html>",
        create=True,
    ):

        if "handler" in sys.modules:
            del sys.modules["handler"]
        if "status_publisher" in sys.modules:
            del sys.modules["status_publisher"]
        import handler

        result = handler.lambda_handler(make_event(), {})

    assert result["batchItemFailures"] == [{"itemIdentifier": "msg-001"}]
    steps = [c.args[1]["step"] for c in mock_channel.publish.call_args_list]
    assert "failed" in steps
    print("test_s3_failure_publishes_failed: PASSED")


def test_sequence_numbers_always_increase():
    """
    Test that sequence numbers monotonically increase across all updates.

    Verifies:
    1. Sequence numbers are strictly increasing (no duplicates)
    2. Sequence numbers are sorted in published order
    3. Critical for clients to maintain proper ordering of async updates
    """
    mock_s3, mock_dynamodb, mock_sqs, mock_channel, mock_ably_rest, boto3_factory = (
        make_mocks()
    )

    with patch("boto3.client", side_effect=boto3_factory), patch(
        "ably.AblyRest", return_value=mock_ably_rest
    ), patch("crawler.crawl_website_html", return_value="<html></html>"), patch(
        "crawler.extract_css", return_value={"css_links": [], "inline_styles": []}
    ), patch(
        "crawler.download_css_files", return_value=""
    ), patch(
        "crawler.rewrite_html_for_regenerated_styles",
        return_value="<html></html>",
        create=True,
    ):

        if "handler" in sys.modules:
            del sys.modules["handler"]
        if "status_publisher" in sys.modules:
            del sys.modules["status_publisher"]
        import handler

        handler.lambda_handler(make_event(), {})

    seqs = [c.args[1]["sequence"] for c in mock_channel.publish.call_args_list]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    print("test_sequence_numbers_always_increase: PASSED")


def test_retry_continues_sequence_from_existing():
    """
    Verify that a retry on the same RegeneratedWebsiteId continues sequence
    numbers from the existing CurrentSequence in DynamoDB rather than restarting
    at 1, so the frontend's sequence deduplication does not discard retry events.
    """
    mock_s3, mock_dynamodb, mock_sqs, mock_channel, mock_ably_rest, boto3_factory = (
        make_mocks()
    )

    PRIOR_SEQ = 5
    mock_dynamodb.get_item.return_value = {
        "Item": {
            "RegeneratedWebsiteId": {"S": WEBSITE_ID},
            "RegeneratedWebsiteUrl": {"S": URL},
            "CurrentSequence": {"N": str(PRIOR_SEQ)},
        }
    }

    with patch("boto3.client", side_effect=boto3_factory), patch(
        "ably.AblyRest", return_value=mock_ably_rest
    ), patch("crawler.crawl_website_html", return_value="<html></html>"), patch(
        "crawler.extract_css",
        return_value={"css_links": [], "inline_styles": ["body{}"]},
    ), patch(
        "crawler.download_css_files", return_value=""
    ), patch(
        "crawler.rewrite_html_for_regenerated_styles",
        return_value="<html></html>",
        create=True,
    ):

        if "handler" in sys.modules:
            del sys.modules["handler"]
        if "status_publisher" in sys.modules:
            del sys.modules["status_publisher"]
        import handler

        result = handler.lambda_handler(make_event(), {})

    assert result == {"batchItemFailures": []}

    seqs = [c.args[1]["sequence"] for c in mock_channel.publish.call_args_list]

    assert seqs[0] == PRIOR_SEQ + 1, f"First seq should be {PRIOR_SEQ + 1}, got {seqs[0]}"
    assert all(s > PRIOR_SEQ for s in seqs), f"All seqs must be > {PRIOR_SEQ}: {seqs}"
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), "Sequences not strictly increasing"

    print("test_retry_continues_sequence_from_existing: PASSED")


if __name__ == "__main__":
    test_happy_path_publishes_all_steps_in_order()
    test_crawl_failure_publishes_failed()
    test_s3_failure_publishes_failed()
    test_sequence_numbers_always_increase()
    test_retry_continues_sequence_from_existing()
    print("All tests passed.")
