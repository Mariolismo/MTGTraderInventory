"""S3 artifact helpers for pricing runs."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def run_prefix(pricing_run_id: str) -> str:
    return f"runs/{pricing_run_id}"


def put_bytes(bucket: str, key: str, body: bytes, *, content_type: str) -> str:
    import boto3

    client = boto3.client("s3")
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    uri = f"s3://{bucket}/{key}"
    logger.info("Wrote %s (%s bytes)", uri, len(body))
    return uri


def put_json(bucket: str, key: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, indent=2).encode("utf-8")
    return put_bytes(bucket, key, body, content_type="application/json")


def get_text(bucket: str, key: str) -> str:
    import boto3

    client = boto3.client("s3")
    obj = client.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read().decode("utf-8")


def get_json(bucket: str, key: str) -> dict[str, Any]:
    return json.loads(get_text(bucket, key))
