import importlib
import json
import os
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch


os.environ["S3_BUCKET_NAME"] = "test-bucket"
os.environ["DYNAMODB_TABLE_NAME"] = "test-table"
os.environ["SQS_QUEUE_URL"] = "https://sqs.us-east-1.amazonaws.com/123/test.fifo"
os.environ["ABLY_SECRET_NAME"] = "test/ably-secret"

WEBSITE_ID = "abc-123"
URL = "https://example.com"
THEME = "dark"
RAW_HTML = "<!DOCTYPE html><html><body><p>Raw crawl</p></body></html>"
INLINE_HTML = "<!DOCTYPE html><html><body><p style=\"color: red\">Processed ✓</p></body></html>"
CSS_SOURCES = [{"kind": "embedded", "css": "p { color: red; }"}]
ALL_CSS = "p { color: red; }"


def make_event(records=None):
    if records is not None:
        return {"Records": records}
    return {
        "Records": [
            {
                "messageId": "msg-001",
                "body": json.dumps(
                    {
                        "RegeneratedWebsiteId": WEBSITE_ID,
                        "RegeneratedWebsiteUrl": URL,
                        "RegenerationTheme": THEME,
                    }
                ),
            }
        ]
    }


def make_mocks():
    mock_s3 = MagicMock()
    mock_dynamodb = MagicMock()
    mock_dynamodb.get_item.return_value = {}
    mock_sqs = MagicMock()
    mock_secrets = MagicMock()
    mock_secrets.get_secret_value.return_value = {
        "SecretString": json.dumps({"AblyApiKey": "fake-key"})
    }

    def boto3_client_factory(service, **kwargs):
        return {
            "s3": mock_s3,
            "dynamodb": mock_dynamodb,
            "sqs": mock_sqs,
            "secretsmanager": mock_secrets,
        }[service]

    mock_channel = MagicMock()
    mock_channel.publish = AsyncMock()
    mock_ably_rest = MagicMock()
    mock_ably_rest.channels.get.return_value = mock_channel
    return (
        mock_s3,
        mock_dynamodb,
        mock_sqs,
        mock_secrets,
        mock_channel,
        mock_ably_rest,
        boto3_client_factory,
    )


@contextmanager
def imported_handler(boto3_factory, mock_ably_rest):
    with patch("boto3.client", side_effect=boto3_factory), patch(
        "ably.AblyRest", return_value=mock_ably_rest
    ):
        sys.modules.pop("handler", None)
        sys.modules.pop("status_publisher", None)
        yield importlib.import_module("handler")


def patch_happy_dependencies(handler, downloaded_images=None):
    return (
        patch.object(handler, "crawl_website_html", return_value=RAW_HTML),
        patch.object(handler, "extract_css", return_value=CSS_SOURCES),
        patch.object(handler, "download_css_files", return_value=ALL_CSS),
        patch.object(handler, "extract_image_urls", return_value=[]),
        patch.object(handler, "download_images", return_value=downloaded_images or {}),
        patch.object(handler, "prepare_inline_html", return_value=INLINE_HTML),
    )


def s3_objects_by_key(mock_s3):
    return {call.kwargs["Key"]: call.kwargs for call in mock_s3.put_object.call_args_list}


def published_steps(mock_channel):
    return [call.args[1]["step"] for call in mock_channel.publish.call_args_list]


def test_happy_path_preserves_artifact_and_queue_contracts():
    mocks = make_mocks()
    mock_s3, mock_dynamodb, mock_sqs, mock_secrets, mock_channel, mock_ably_rest, factory = mocks
    downloaded = {
        "https://example.com/logo.png": {
            "content": b"png-bytes",
            "content_type": "image/png",
        }
    }

    with imported_handler(factory, mock_ably_rest) as handler:
        crawl, extract, download_css, image_urls, download_images, prepare = patch_happy_dependencies(
            handler, downloaded
        )
        with crawl, extract, download_css, image_urls, download_images, prepare as prepare_mock:
            result = handler.lambda_handler(make_event(), {})

    assert result == {"batchItemFailures": []}
    assert published_steps(mock_channel) == [
        "received",
        "crawling_html",
        "extracting_css",
        "extracting_images",
        "queueing_ai",
    ]
    sequences = [call.args[1]["sequence"] for call in mock_channel.publish.call_args_list]
    assert sequences == [1, 2, 3, 4, 5]
    assert all(call.args[1]["status"] == "processing" for call in mock_channel.publish.call_args_list)

    objects = s3_objects_by_key(mock_s3)
    assert set(objects) == {
        f"{WEBSITE_ID}/images/img-0.png",
        f"{WEBSITE_ID}/index.html",
        f"{WEBSITE_ID}/original-styles.css",
    }
    assert not any(key.endswith("Regenerated-Index.html") for key in objects)
    assert objects[f"{WEBSITE_ID}/index.html"]["Body"] == INLINE_HTML.encode("utf-8")
    assert objects[f"{WEBSITE_ID}/index.html"]["Body"] != RAW_HTML.encode("utf-8")
    assert objects[f"{WEBSITE_ID}/index.html"]["ContentType"] == "text/html; charset=utf-8"
    assert objects[f"{WEBSITE_ID}/original-styles.css"]["Body"] == ALL_CSS.encode("utf-8")
    assert objects[f"{WEBSITE_ID}/original-styles.css"]["ContentType"] == "text/css; charset=utf-8"
    assert isinstance(objects[f"{WEBSITE_ID}/index.html"]["Body"], bytes)
    assert isinstance(objects[f"{WEBSITE_ID}/original-styles.css"]["Body"], bytes)
    prepare_mock.assert_called_once_with(
        RAW_HTML,
        ALL_CSS,
        URL,
        {"https://example.com/logo.png": "./images/img-0.png"},
    )
    mock_dynamodb.put_item.assert_called_once()
    mock_secrets.get_secret_value.assert_called_once_with(SecretId="test/ably-secret")

    mock_sqs.send_message.assert_called_once()
    queue_call = mock_sqs.send_message.call_args.kwargs
    assert queue_call["QueueUrl"] == os.environ["SQS_QUEUE_URL"]
    assert queue_call["MessageDeduplicationId"] == WEBSITE_ID
    assert json.loads(queue_call["MessageBody"]) == {
        "RegeneratedWebsiteId": WEBSITE_ID,
        "RegeneratedWebsiteUrl": URL,
        "RegenerationTheme": THEME,
    }


def test_processor_failure_is_a_batch_failure_and_does_not_queue_downstream():
    mocks = make_mocks()
    mock_s3, _, mock_sqs, _, mock_channel, mock_ably_rest, factory = mocks

    with imported_handler(factory, mock_ably_rest) as handler:
        crawl, extract, download_css, image_urls, download_images, _ = patch_happy_dependencies(handler)
        with crawl, extract, download_css, image_urls, download_images, patch.object(
            handler, "prepare_inline_html", side_effect=RuntimeError("premailer failed")
        ):
            result = handler.lambda_handler(make_event(), {})

    assert result == {"batchItemFailures": [{"itemIdentifier": "msg-001"}]}
    assert published_steps(mock_channel)[-1] == "failed"
    failed = mock_channel.publish.call_args_list[-1].args[1]
    assert failed["status"] == "failed"
    assert failed["error"] == "premailer failed"
    assert mock_sqs.send_message.call_count == 0
    assert not any(
        call.kwargs["Key"].endswith("index.html") for call in mock_s3.put_object.call_args_list
    )


def test_crawl_failure_publishes_failed():
    mocks = make_mocks()
    _, _, mock_sqs, _, mock_channel, mock_ably_rest, factory = mocks

    with imported_handler(factory, mock_ably_rest) as handler, patch.object(
        handler, "crawl_website_html", side_effect=RuntimeError("timeout")
    ):
        result = handler.lambda_handler(make_event(), {})

    assert result == {"batchItemFailures": [{"itemIdentifier": "msg-001"}]}
    assert published_steps(mock_channel) == ["received", "crawling_html", "failed"]
    assert mock_sqs.send_message.call_count == 0


def test_oversized_css_file_publishes_failed():
    mocks = make_mocks()
    _, _, mock_sqs, _, mock_channel, mock_ably_rest, factory = mocks
    error = ValueError("CSS file exceeds maximum allowed size")

    with imported_handler(factory, mock_ably_rest) as handler, patch.object(
        handler, "crawl_website_html", return_value=RAW_HTML
    ), patch.object(handler, "extract_css", return_value=CSS_SOURCES), patch.object(
        handler, "download_css_files", side_effect=error
    ):
        result = handler.lambda_handler(make_event(), {})

    assert result == {"batchItemFailures": [{"itemIdentifier": "msg-001"}]}
    assert published_steps(mock_channel)[-1] == "failed"
    assert "exceeds maximum allowed size" in mock_channel.publish.call_args_list[-1].args[1]["error"]
    assert mock_sqs.send_message.call_count == 0


def test_s3_failure_publishes_failed_and_does_not_queue():
    mocks = make_mocks()
    mock_s3, _, mock_sqs, _, mock_channel, mock_ably_rest, factory = mocks
    mock_s3.put_object.side_effect = RuntimeError("S3 unavailable")

    with imported_handler(factory, mock_ably_rest) as handler:
        patches = patch_happy_dependencies(handler)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = handler.lambda_handler(make_event(), {})

    assert result == {"batchItemFailures": [{"itemIdentifier": "msg-001"}]}
    assert published_steps(mock_channel)[-1] == "failed"
    assert mock_sqs.send_message.call_count == 0


def test_sequence_numbers_are_monotonic_and_retry_from_existing_value():
    mocks = make_mocks()
    _, mock_dynamodb, _, _, mock_channel, mock_ably_rest, factory = mocks
    mock_dynamodb.get_item.return_value = {
        "Item": {"CurrentSequence": {"N": "5"}}
    }

    with imported_handler(factory, mock_ably_rest) as handler:
        patches = patch_happy_dependencies(handler)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = handler.lambda_handler(make_event(), {})

    assert result == {"batchItemFailures": []}
    sequences = [call.args[1]["sequence"] for call in mock_channel.publish.call_args_list]
    assert sequences == [6, 7, 8, 9, 10]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
