import json
import logging
import math
import os
import urllib.request
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

import boto3
from botocore.exceptions import ClientError

try:
    from sgp4.api import Satrec, jday
    from sgp4.omm import initialize as initialize_omm
except ImportError:
    Satrec = None
    jday = None
    initialize_omm = None


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


def parse_and_validate_satellite_json(data: bytes) -> list:
    parsed = json.loads(data.decode("utf-8"))

    if not isinstance(parsed, list):
        raise RuntimeError("CelesTrak response was not a JSON list")

    if len(parsed) == 0:
        raise RuntimeError("CelesTrak response contained zero satellites")

    return parsed


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


def gmst_radians(jd: float) -> float:
    t = (jd - 2451545.0) / 36525.0

    seconds = (
        67310.54841
        + (876600.0 * 3600 + 8640184.812866) * t
        + 0.093104 * t * t
        - 0.0000062 * t * t * t
    )

    degrees = (seconds / 240.0) % 360.0
    return math.radians(degrees)


def eci_to_ecef(position_km: tuple, gmst: float) -> tuple:
    x, y, z = position_km

    cos_gmst = math.cos(gmst)
    sin_gmst = math.sin(gmst)

    x_ecef = cos_gmst * x + sin_gmst * y
    y_ecef = -sin_gmst * x + cos_gmst * y
    z_ecef = z

    return x_ecef, y_ecef, z_ecef


def ecef_to_geodetic(position_km: tuple) -> dict:
    x, y, z = position_km

    a = 6378.137
    f = 1.0 / 298.257223563
    e2 = f * (2.0 - f)

    lon = math.atan2(y, x)
    p = math.sqrt(x * x + y * y)

    lat = math.atan2(z, p * (1.0 - e2))

    for _ in range(7):
        sin_lat = math.sin(lat)
        n = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
        alt = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1.0 - e2 * n / (n + alt)))

    sin_lat = math.sin(lat)
    n = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
    alt = p / math.cos(lat) - n

    return {
        "latitude": round(math.degrees(lat), 6),
        "longitude": round(math.degrees(lon), 6),
        "altitude_km": round(alt, 3),
    }


def calculate_skygrid_position(sat: dict, timestamp: datetime) -> dict:
    if Satrec is None or jday is None or initialize_omm is None:
        raise RuntimeError("sgp4 dependency is not installed")

    satrec = Satrec()

    fields = {
        key: str(value)
        for key, value in sat.items()
        if value is not None
    }

    initialize_omm(satrec, fields)

    jd, fr = jday(
        timestamp.year,
        timestamp.month,
        timestamp.day,
        timestamp.hour,
        timestamp.minute,
        timestamp.second + timestamp.microsecond / 1_000_000,
    )

    error_code, position_km, velocity_km_s = satrec.sgp4(jd, fr)

    if error_code != 0:
        raise RuntimeError(f"SGP4 error code {error_code}")

    gmst = gmst_radians(jd + fr)
    ecef_km = eci_to_ecef(position_km, gmst)
    geo = ecef_to_geodetic(ecef_km)

    return {
        "position_calculated_at": timestamp.isoformat().replace("+00:00", "Z"),
        "latitude": geo["latitude"],
        "longitude": geo["longitude"],
        "altitude_km": geo["altitude_km"],
        "propagation_model": "SGP4",
        "coordinate_system": "WGS84",
        "source": "SkyGrid Lambda enrichment",
    }


def enrich_satellites(satellites: list, timestamp: datetime) -> tuple[list, int, int]:
    enriched = []
    success_count = 0
    failure_count = 0

    for sat in satellites:
        enriched_sat = dict(sat)

        try:
            enriched_sat["SKYGRID"] = calculate_skygrid_position(sat, timestamp)
            success_count += 1
        except Exception as e:
            failure_count += 1
            logger.warning(
                "Failed to enrich satellite %s: %s",
                sat.get("OBJECT_NAME", "UNKNOWN"),
                str(e),
            )

            enriched_sat["SKYGRID"] = {
                "position_calculated_at": timestamp.isoformat().replace("+00:00", "Z"),
                "position_available": False,
                "error": "Position calculation failed",
                "source": "SkyGrid Lambda enrichment",
            }

        enriched.append(enriched_sat)

    return enriched, success_count, failure_count


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

    raw_data = fetch_celestrak(celestrak_url)
    satellites = parse_and_validate_satellite_json(raw_data)

    enriched_satellites, enriched_count, enrichment_failures = enrich_satellites(
        satellites,
        now,
    )

    new_data = json.dumps(
        enriched_satellites,
        separators=(",", ":"),
    ).encode("utf-8")

    archived = False

    if latest_exists(bucket, latest_key):
        copy_latest_to_archive(bucket, latest_key, destination_archive_key)
        archived = True
        logger.info("Archived existing latest to %s", destination_archive_key)
    else:
        logger.info("No existing latest file found. Archive step skipped.")

    write_latest(bucket, latest_key, new_data)

    logger.info(
        "Updated latest file with %s satellites. Enriched: %s. Failures: %s",
        len(enriched_satellites),
        enriched_count,
        enrichment_failures,
    )

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": "SkyGrid CelesTrak collection complete",
                "satelliteCount": len(enriched_satellites),
                "skygridEnrichedCount": enriched_count,
                "skygridEnrichmentFailures": enrichment_failures,
                "archivedPreviousLatest": archived,
                "latestKey": latest_key,
                "archiveKey": destination_archive_key if archived else None,
            }
        ),
    }