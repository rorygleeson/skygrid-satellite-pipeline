import csv
import gzip
import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")


DEFAULT_STARLINK_LOOKUP_KEY = "data/satcat/latest/starlink_lookup.json"


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def fetch_celestrak(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SkyGrid/1.0 SATCAT collector"},
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"CelesTrak returned HTTP {response.status}")
            return response.read()
    except HTTPError as e:
        raise RuntimeError(f"CelesTrak HTTP error: {e.code}") from e
    except URLError as e:
        raise RuntimeError(f"CelesTrak URL error: {e.reason}") from e


def normalise_key(key: str) -> str:
    key = key.strip().lower()
    key = re.sub(r"[^a-z0-9]+", "_", key)
    return key.strip("_")


def parse_satcat_csv(data: bytes) -> list:
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines())

    rows = []
    for row in reader:
        clean_row = {
            normalise_key(k): (v.strip() if isinstance(v, str) else v)
            for k, v in row.items()
            if k is not None
        }
        rows.append(clean_row)

    if not rows:
        raise RuntimeError("CelesTrak SATCAT response contained zero rows")

    return rows


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
        f"{timestamp:%Y%m%dT%H%M%SZ}.csv"
    )


def analytics_key(prefix: str, timestamp: datetime) -> str:
    return (
        f"{prefix}/"
        f"year={timestamp:%Y}/"
        f"month={timestamp:%m}/"
        f"day={timestamp:%d}/"
        f"snapshot_{timestamp:%Y%m%dT%H%M%SZ}.jsonl.gz"
    )


def copy_latest_to_archive(bucket: str, latest_key: str, destination_key: str) -> None:
    s3.copy_object(
        Bucket=bucket,
        CopySource={"Bucket": bucket, "Key": latest_key},
        Key=destination_key,
        ContentType="text/csv",
        MetadataDirective="REPLACE",
    )


def write_latest(bucket: str, latest_key: str, data: bytes) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=latest_key,
        Body=data,
        ContentType="text/csv",
        CacheControl="max-age=3600",
    )


def write_json(bucket: str, key: str, data) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
        CacheControl="max-age=3600",
    )


def write_analytics_jsonl_gzip(bucket: str, key: str, rows: list) -> None:
    jsonl = "\n".join(
        json.dumps(row, separators=(",", ":"))
        for row in rows
    ) + "\n"

    compressed = gzip.compress(jsonl.encode("utf-8"))

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=compressed,
        ContentType="application/x-ndjson",
        ContentEncoding="gzip",
        CacheControl="max-age=3600",
    )


def build_analytics_rows(satcat_rows: list, collection_time: datetime) -> list:
    collection_time_str = collection_time.isoformat().replace("+00:00", "Z")
    rows = []

    for row in satcat_rows:
        row["collection_time"] = collection_time_str
        row["dataset"] = "satcat"
        row["source"] = "CelesTrak SATCAT"
        rows.append(row)

    return rows


def first_value(row: dict, keys: list[str]):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def build_starlink_lookup(satcat_rows: list) -> dict:
    """
    Build a compact frontend lookup keyed by NORAD catalog ID.

    This is additive only: it does not alter the raw SATCAT CSV, archive files,
    or analytics JSONL. index.html can use this file later to enrich the
    satellite details panel.
    """
    lookup = {}

    for row in satcat_rows:
        object_name = first_value(row, ["object_name", "satname", "satellite_name"]) or ""
        norad = first_value(row, ["norad_cat_id", "norad_number", "catalog_number", "satcat"])

        if not norad:
            continue

        if "starlink" not in str(object_name).lower():
            continue

        lookup[str(norad)] = {
            "object_name": object_name or None,
            "norad_cat_id": str(norad),
            "object_id": first_value(row, ["object_id", "international_designator", "int_des", "intl_des"]),
            "launch_date": first_value(row, ["launch_date", "launch", "launchdate"]),
            "launch_site": first_value(row, ["launch_site", "site", "launchsite"]),
            "object_type": first_value(row, ["object_type", "objecttype", "type"]),
        }

    return lookup


def lambda_handler(event, context):
    bucket = env("BUCKET_NAME")
    latest_key = env("LATEST_KEY")
    archive_prefix = env("ARCHIVE_PREFIX").rstrip("/")
    analytics_prefix = os.environ.get(
        "ANALYTICS_PREFIX",
        "analytics/satcat_jsonl",
    ).rstrip("/")
    celestrak_url = env("CELESTRAK_URL")
    starlink_lookup_key = os.environ.get(
        "STARLINK_LOOKUP_KEY",
        DEFAULT_STARLINK_LOOKUP_KEY,
    )

    now = datetime.now(timezone.utc)
    destination_archive_key = archive_key(archive_prefix, now)
    destination_analytics_key = analytics_key(analytics_prefix, now)

    logger.info("SkyGrid CelesTrak SATCAT collector started")
    logger.info("Bucket: %s", bucket)
    logger.info("Latest key: %s", latest_key)
    logger.info("Archive key: %s", destination_archive_key)
    logger.info("Analytics key: %s", destination_analytics_key)
    logger.info("Starlink lookup key: %s", starlink_lookup_key)

    raw_data = fetch_celestrak(celestrak_url)
    satcat_rows = parse_satcat_csv(raw_data)

    archived = False

    if latest_exists(bucket, latest_key):
        copy_latest_to_archive(bucket, latest_key, destination_archive_key)
        archived = True
        logger.info("Archived existing latest to %s", destination_archive_key)
    else:
        logger.info("No existing latest file found. Archive step skipped.")

    write_latest(bucket, latest_key, raw_data)

    analytics_rows = build_analytics_rows(satcat_rows, now)
    write_analytics_jsonl_gzip(bucket, destination_analytics_key, analytics_rows)

    starlink_lookup = build_starlink_lookup(satcat_rows)
    write_json(bucket, starlink_lookup_key, starlink_lookup)

    logger.info(
        "SATCAT collection complete. Rows: %s. Starlink lookup entries: %s",
        len(analytics_rows),
        len(starlink_lookup),
    )

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": "SkyGrid CelesTrak SATCAT collection complete",
                "satcatRowCount": len(analytics_rows),
                "archivedPreviousLatest": archived,
                "latestKey": latest_key,
                "archiveKey": destination_archive_key if archived else None,
                "analyticsKey": destination_analytics_key,
                "analyticsRowCount": len(analytics_rows),
                "starlinkLookupKey": starlink_lookup_key,
                "starlinkLookupCount": len(starlink_lookup),
            }
        ),
    }
