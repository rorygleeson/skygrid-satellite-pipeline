import gzip
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

NOAA_KP_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
NOAA_SCALES_URL = "https://services.swpc.noaa.gov/products/noaa-scales.json"
NOAA_SOLAR_WIND_URL = "https://services.swpc.noaa.gov/products/solar-wind/plasma-1-day.json"


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def hour_floor(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def fetch_json(url: str):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SkyGrid/1.0 space weather collector",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise RuntimeError(f"NOAA returned HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        raise RuntimeError(f"NOAA HTTP error: {e.code}") from e
    except URLError as e:
        raise RuntimeError(f"NOAA URL error: {e.reason}") from e


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


def analytics_key(prefix: str, timestamp: datetime) -> str:
    return (
        f"{prefix}/"
        f"year={timestamp:%Y}/"
        f"month={timestamp:%m}/"
        f"day={timestamp:%d}/"
        f"hour={timestamp:%H}/"
        f"snapshot_{timestamp:%Y%m%dT%H%M%SZ}.jsonl.gz"
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
        CacheControl="max-age=60",
    )


def write_analytics_jsonl_gzip(bucket: str, key: str, row: dict) -> None:
    jsonl = json.dumps(row, separators=(",", ":")) + "\n"
    compressed = gzip.compress(jsonl.encode("utf-8"))

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=compressed,
        ContentType="application/x-ndjson",
        ContentEncoding="gzip",
        CacheControl="max-age=300",
    )


def to_float(value):
    try:
        if value in (None, "", "null"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def scale_level(prefix, value):
    if value in (None, "", "null"):
        return None

    text = str(value).strip()

    if text.upper().startswith(prefix):
        return text.upper()

    return f"{prefix}{text}"


def latest_kp(kp_data):
    if not isinstance(kp_data, list) or not kp_data:
        return None

    latest = kp_data[-1]

    return {
        "kp_time_tag": latest.get("time_tag"),
        "kp_index": to_float(latest.get("kp_index")),
        "kp_observed": latest.get("observed"),
    }


def noaa_scale_value(scales, key):
    value = scales.get(key)
    if isinstance(value, dict):
        return value.get("Scale") or value.get("scale") or value.get("level")
    return value


def extract_noaa_scales(scales_data):
    if not isinstance(scales_data, dict):
        return {}

    current = scales_data.get("0") or scales_data.get("current") or scales_data

    return {
        "geomagnetic_storm_level": scale_level("G", noaa_scale_value(current, "G")),
        "radio_blackout_level": scale_level("R", noaa_scale_value(current, "R")),
        "radiation_storm_level": scale_level("S", noaa_scale_value(current, "S")),
    }


def latest_solar_wind(plasma_data):
    if not isinstance(plasma_data, list) or len(plasma_data) < 2:
        return {}

    headers = plasma_data[0]
    rows = plasma_data[1:]

    if not rows:
        return {}

    latest = rows[-1]
    row = dict(zip(headers, latest))

    return {
        "solar_wind_time_tag": row.get("time_tag"),
        "solar_wind_density_cm3": to_float(row.get("density")),
        "solar_wind_speed_km_s": to_float(row.get("speed")),
        "solar_wind_temperature_k": to_float(row.get("temperature")),
    }


def build_space_weather_record(collection_time: datetime) -> dict:
    kp_data = fetch_json(NOAA_KP_URL)
    scales_data = fetch_json(NOAA_SCALES_URL)
    plasma_data = fetch_json(NOAA_SOLAR_WIND_URL)

    record = {
        "collection_time": iso_z(collection_time),
        "observation_hour": iso_z(hour_floor(collection_time)),
        "source": "NOAA_SWPC",
    }

    record.update(latest_kp(kp_data) or {})
    record.update(extract_noaa_scales(scales_data))
    record.update(latest_solar_wind(plasma_data))

    record["year"] = collection_time.strftime("%Y")
    record["month"] = collection_time.strftime("%m")
    record["day"] = collection_time.strftime("%d")
    record["hour"] = collection_time.strftime("%H")

    return record


def lambda_handler(event, context):
    bucket = env("BUCKET_NAME")
    latest_key = env("LATEST_KEY")
    archive_prefix = env("ARCHIVE_PREFIX").rstrip("/")
    analytics_prefix = os.environ.get(
        "ANALYTICS_PREFIX",
        "analytics/space_weather_jsonl",
    ).rstrip("/")

    now = utc_now()
    destination_archive_key = archive_key(archive_prefix, now)
    destination_analytics_key = analytics_key(analytics_prefix, now)

    logger.info("SkyGrid NOAA space weather collector started")
    logger.info("Bucket: %s", bucket)
    logger.info("Latest key: %s", latest_key)
    logger.info("Archive key: %s", destination_archive_key)
    logger.info("Analytics key: %s", destination_analytics_key)

    record = build_space_weather_record(now)

    new_data = json.dumps(
        record,
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")

    archived = False

    if latest_exists(bucket, latest_key):
        copy_latest_to_archive(bucket, latest_key, destination_archive_key)
        archived = True
        logger.info("Archived existing latest to %s", destination_archive_key)
    else:
        logger.info("No existing latest file found. Archive step skipped.")

    write_latest(bucket, latest_key, new_data)
    write_analytics_jsonl_gzip(bucket, destination_analytics_key, record)

    logger.info("Wrote latest space weather file")
    logger.info("Wrote analytics JSONL file to %s", destination_analytics_key)

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": "SkyGrid NOAA space weather collection complete",
                "archivedPreviousLatest": archived,
                "latestKey": latest_key,
                "archiveKey": destination_archive_key if archived else None,
                "analyticsKey": destination_analytics_key,
                "record": record,
            }
        ),
    }