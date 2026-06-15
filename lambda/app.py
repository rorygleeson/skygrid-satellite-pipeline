import json
import logging
import os
import urllib.request
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

import boto3
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def fetch_celestrak(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SkyGrid/1.0 satellite collector"},
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise RuntimeError(f"CelesTrak returned HTTP {response.status}")
            return response.read()
    except HTTPError as e:
        raise RuntimeError(f"CelesTrak HTTP error: {e.code}") from e
    except URLError as e:
        raise RuntimeError(f"CelesTrak URL error: {e.reason}") from e


def validate_satellite_json(data: bytes) -> int:
    parsed = json.loads(data.decode("utf-8"))

    if not isinstance(parsed, list):
        raise RuntimeError("CelesTrak response was not a JSON list")

    if len(parsed) == 0:
        raise RuntimeError("CelesTrak response contained zero satellites")

    return len(parsed)


def latest_exists(bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def archive_key(prefix: str, timestamp: datetime) -> str:
    return (
        f"{prefix}/"
        f"{timestamp:%Y}/"
        f"{timestamp:%m}/"
        f"{timestamp:%d}/"
        f"{timestamp:%Y%m%dT%H%M%SZ}.json"
    )


def copy_latest_to_archive(bucket: str, latest_key: str, destination_key: str) -> None:
    s3.copy_object(
        Bucket=bucket,
        CopySource={"Bucket": bucket, "Key": latest_key},
        Key=destination_key,
        ContentType="application/json",
        MetadataDirective="REPLACE",
    )


def write_latest(bucket: str, latest_key: str, data: bytes) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=latest_key,
        Body=data,
        ContentType="application/json",
        CacheControl="max-age=30",
    )


def lambda_handler(event, context):
    bucket = env("BUCKET_NAME")
    latest_key = env("LATEST_KEY")
    archive_prefix = env("ARCHIVE_PREFIX").rstrip("/")
    celestrak_url = env("CELESTRAK_URL")

    now = datetime.now(timezone.utc)
    destination_archive_key = archive_key(archive_prefix, now)

    logger.info("SkyGrid CelesTrak collector started")
    logger.info("Bucket: %s", bucket)
    logger.info("Latest key: %s", latest_key)
    logger.info("Archive key: %s", destination_archive_key)

    new_data = fetch_celestrak(celestrak_url)
    satellite_count = validate_satellite_json(new_data)

    archived = False

    if latest_exists(bucket, latest_key):
        copy_latest_to_archive(bucket, latest_key, destination_archive_key)
        archived = True
        logger.info("Archived existing latest to %s", destination_archive_key)
    else:
        logger.info("No existing latest file found. Archive step skipped.")

    write_latest(bucket, latest_key, new_data)
    logger.info("Updated latest file with %s satellites", satellite_count)

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": "SkyGrid CelesTrak collection complete",
                "satelliteCount": satellite_count,
                "archivedPreviousLatest": archived,
                "latestKey": latest_key,
                "archiveKey": destination_archive_key if archived else None,
            }
        ),
    }