"""
Status publisher module for real-time website crawler updates.

This module provides functionality to publish status updates about webpage
regeneration progress to Ably channels and persist status to DynamoDB.
"""
import asyncio
import json
import os
from datetime import datetime, timezone

import boto3
from ably import AblyRest

_secrets_client = None
_ably_client = None
_dynamodb_client = None
_ably_api_key = None


def _get_secrets_client():
    """Get or initialize the AWS Secrets Manager client."""
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def _get_ably_api_key():
    """Retrieve the Ably API key from AWS Secrets Manager."""
    global _ably_api_key
    if _ably_api_key is None:
        secret_name = os.environ["ABLY_SECRET_NAME"]
        response = _get_secrets_client().get_secret_value(SecretId=secret_name)
        _ably_api_key = json.loads(response["SecretString"])["AblyApiKey"]
    return _ably_api_key


def _get_ably_client():
    """Get or initialize the Ably REST client."""
    global _ably_client
    if _ably_client is None:
        _ably_client = AblyRest(key=_get_ably_api_key())
    return _ably_client


def _get_dynamodb_client():
    """Get or initialize the DynamoDB client."""
    global _dynamodb_client
    if _dynamodb_client is None:
        _dynamodb_client = boto3.client("dynamodb")
    return _dynamodb_client


def publish_status_update(
    website_id: str,
    website_url: str,
    phase: str,
    step: str,
    status: str,
    message: str,
    sequence: int,
    publisher: str = "webpage-crawler-lambda",
    result_url: str = None,
    error: str = None,
) -> dict:
    """
    Publish a status update for webpage regeneration.

    Publishes a real-time status update to Ably and persists the update
    to DynamoDB for audit trail and current state tracking.

    Args:
        website_id: Unique identifier for the website being regenerated.
        phase: Current phase of the regeneration process (e.g., 'crawling', 'processing').
        step: Current step within the phase.
        status: Status indicator (e.g., 'in_progress', 'completed', 'failed').
        message: Human-readable message describing the current state.
        sequence: Monotonically increasing sequence number for ordering updates.
        publisher: Identifier of the service publishing the update.
                   Defaults to 'webpage-crawler-lambda'.
        result_url: Optional URL to access the regeneration results.
        error: Optional error message if the status indicates failure.

    Returns:
        dict: The payload that was published, including the generated timestamp.
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    payload = {
        "websiteId": website_id,
        "phase": phase,
        "step": step,
        "status": status,
        "message": message,
        "sequence": sequence,
        "publisher": publisher,
        "timestamp": timestamp,
        "resultUrl": result_url,
        "error": error,
    }

    channel = _get_ably_client().channels.get(f"regeneration:{website_id}")
    asyncio.run(channel.publish("regeneration-status", payload))

    update_expr = (
        "SET CurrentPhase = :phase, CurrentStep = :step, "
        "RegenerationStatus = :status, CurrentSequence = :seq, "
        "LastUpdatedAt = :ts"
    )
    expr_vals = {
        ":phase": {"S": phase},
        ":step": {"S": step},
        ":status": {"S": status},
        ":seq": {"N": str(sequence)},
        ":ts": {"S": timestamp},
    }

    if result_url is not None:
        update_expr += ", ResultUrl = :result_url"
        expr_vals[":result_url"] = {"S": result_url}

    if error is not None:
        update_expr += ", ErrorMessage = :error"
        expr_vals[":error"] = {"S": error}

    _get_dynamodb_client().update_item(
        TableName=os.environ["DYNAMODB_TABLE_NAME"],
        Key={
            "RegeneratedWebsiteId": {"S": website_id},
            "RegeneratedWebsiteUrl": {"S": website_url},
        },
        UpdateExpression=update_expr,
        ExpressionAttributeValues=expr_vals,
    )

    return payload
