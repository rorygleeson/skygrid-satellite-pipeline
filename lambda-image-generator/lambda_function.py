import json
import os
import random
import time
import urllib.request
from datetime import datetime
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

import boto3


BUCKET = "skygrid.io"

MODEL_OWNER = "black-forest-labs"
MODEL_NAME = "flux-schnell"

s3 = boto3.client("s3")


def strip_code_fence(text):
    return text.replace("```json", "").replace("```", "").strip()


def read_json_s3(key):
    response = s3.get_object(Bucket=BUCKET, Key=key)
    return json.loads(response["Body"].read().decode("utf-8"))


def write_json_s3(key, data):
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )


def http_json(url, method="GET", payload=None, token=None, max_retries=6):
    headers = {"Content-Type": "application/json"}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    last_error = None

    for attempt in range(max_retries):
        request = urllib.request.Request(
            url=url,
            data=body,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))

        except HTTPError as e:
            last_error = e

            if e.code != 429:
                error_body = e.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {e.code}: {error_body}") from e

            retry_after = e.headers.get("Retry-After")
            if retry_after:
                sleep_seconds = int(retry_after)
            else:
                sleep_seconds = min(60, (2 ** attempt) + random.uniform(0, 2))

            time.sleep(sleep_seconds)

    raise RuntimeError(f"Replicate API rate limited after {max_retries} retries") from last_error


def download_bytes(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def generate_image(prompt, token):
    create_url = f"https://api.replicate.com/v1/models/{MODEL_OWNER}/{MODEL_NAME}/predictions"

    prediction = http_json(
        create_url,
        method="POST",
        token=token,
        payload={
            "input": {
                "prompt": prompt,
                "num_outputs": 1,
            }
        },
    )

    get_url = prediction["urls"]["get"]

    for _ in range(90):
        prediction = http_json(get_url, method="GET", token=token)
        status = prediction.get("status")

        if status == "succeeded":
            break

        if status in ["failed", "canceled"]:
            raise RuntimeError(json.dumps(prediction, indent=2))

        time.sleep(2)
    else:
        raise TimeoutError("Replicate image generation timed out")

    output = prediction.get("output")

    if isinstance(output, list):
        image_url = output[0]
    else:
        image_url = output

    if not image_url:
        raise RuntimeError("Replicate returned no image URL")

    return download_bytes(image_url)


def get_inner_package(wrapper):
    text = wrapper["response"]["result"]["content"][0]["text"]
    return json.loads(strip_code_fence(text))


def set_inner_package(wrapper, inner):
    wrapper["response"]["result"]["content"][0]["text"] = json.dumps(
        inner,
        indent=2,
        ensure_ascii=False,
    )


def get_melbourne_today_parts():
    today = datetime.now(ZoneInfo("Australia/Melbourne"))
    return today.strftime("%Y"), today.strftime("%m"), today.strftime("%d")


def get_date_parts_from_event(event):
    if isinstance(event, dict) and event.get("date"):
        date_text = event["date"]
        try:
            parsed = datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("date must be in YYYY-MM-DD format") from exc

        return parsed.strftime("%Y"), parsed.strftime("%m"), parsed.strftime("%d")

    return get_melbourne_today_parts()


def build_archive_package_key(yyyy, mm, dd):
    return f"published/news-site/archive/{yyyy}/{mm}/{dd}/package.json"


def build_jobs(inner):
    jobs = [
        {
            "name": "hero",
            "prompt": inner["hero"]["image_prompt"],
            "filename": "hero.webp",
        }
    ]

    for story in inner["stories"]:
        story_number = story["story_number"]
        jobs.append(
            {
                "name": f"story_{story_number}",
                "prompt": story["image_prompt"],
                "filename": f"story_{story_number}.webp",
                "story_number": story_number,
            }
        )

    return jobs


def apply_image_url(inner, job, image_url):
    if job["name"] == "hero":
        inner["hero"]["image_url"] = image_url
        return

    story_number = job["story_number"]

    for story in inner["stories"]:
        if story.get("story_number") == story_number:
            story["image_url"] = image_url
            return

    raise RuntimeError(f"Could not find story_number {story_number}")


def has_all_image_urls(inner):
    if not inner.get("hero", {}).get("image_url"):
        return False

    stories = inner.get("stories", [])
    if not stories:
        return False

    return all(story.get("image_url") for story in stories)


def lambda_handler(event, context):
    token = os.environ.get("REPLICATE_API_TOKEN")
    if not token:
        raise RuntimeError("Missing REPLICATE_API_TOKEN environment variable")

    if event is None:
        event = {}

    force_regenerate = bool(event.get("force_regenerate", False)) if isinstance(event, dict) else False

    yyyy, mm, dd = get_date_parts_from_event(event)
    archive_package_key = build_archive_package_key(yyyy, mm, dd)
    archive_image_prefix = f"published/news-site/archive/{yyyy}/{mm}/{dd}/images"

    wrapper = read_json_s3(archive_package_key)
    inner = get_inner_package(wrapper)

    if has_all_image_urls(inner) and not force_regenerate:
        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Images already generated. Skipped.",
                    "archive_package_key": archive_package_key,
                    "date": f"{yyyy}-{mm}-{dd}",
                }
            ),
        }

    jobs = build_jobs(inner)
    images_written = []

    for job in jobs:
        enhanced_prompt = (
            job["prompt"]
            + ", realistic aerospace photography, "
            "cinematic space journalism, "
            "no text, "
            "no words, "
            "no letters, "
            "no numbers, "
            "no captions, "
            "no labels, "
            "no branding, "
            "no logos, "
            "no watermark, "
            "no charts, "
            "no graphs, "
            "no dashboard, "
            "no infographic, "
            "no HUD, "
            "no UI, "
            "image only"
        )

        image_bytes = generate_image(enhanced_prompt, token)
        archive_key = f"{archive_image_prefix}/{job['filename']}"

        s3.put_object(
            Bucket=BUCKET,
            Key=archive_key,
            Body=image_bytes,
            ContentType="image/webp",
        )

        image_url = "/" + archive_key
        apply_image_url(inner, job, image_url)

        images_written.append(
            {
                "name": job["name"],
                "archive_key": archive_key,
                "image_url": image_url,
            }
        )

    set_inner_package(wrapper, inner)
    write_json_s3(archive_package_key, wrapper)

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": "SkyGrid news-site images generated",
                "model": f"{MODEL_OWNER}/{MODEL_NAME}",
                "date": f"{yyyy}-{mm}-{dd}",
                "archive_package_key": archive_package_key,
                "images_written": images_written,
            }
        ),
    }
