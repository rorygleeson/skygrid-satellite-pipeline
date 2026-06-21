import random
from urllib.error import HTTPError
import json
import os
import time
import urllib.request

import boto3


BUCKET = "skygrid.io"
LATEST_PACKAGE_KEY = "published/news-site/latest/package.json"

MODEL_OWNER = "black-forest-labs"
MODEL_NAME = "flux-schnell"

s3 = boto3.client("s3")


def strip_code_fence(text):
    return text.replace("```json", "").replace("```", "").strip()


def deep_copy(value):
    return json.loads(json.dumps(value))


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
                "num_outputs": 1
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


def get_archive_package_key(wrapper):
    run_id = wrapper["run_id"]
    generated_at = wrapper["generated_at"]

    yyyy = generated_at[0:4]
    mm = generated_at[5:7]
    dd = generated_at[8:10]

    return f"published/news-site/archive/{yyyy}/{mm}/{dd}/package_{run_id}.json"


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
        jobs.append({
            "name": f"story_{story_number}",
            "prompt": story["image_prompt"],
            "filename": f"story_{story_number}.webp",
            "story_number": story_number,
        })

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


def lambda_handler(event, context):
    token = os.environ.get("REPLICATE_API_TOKEN")
    if not token:
        raise RuntimeError("Missing REPLICATE_API_TOKEN environment variable")

    force_regenerate = False
    if isinstance(event, dict):
        force_regenerate = bool(event.get("force_regenerate", False))

    latest_wrapper = read_json_s3(LATEST_PACKAGE_KEY)
    latest_inner = get_inner_package(latest_wrapper)

    if latest_inner.get("hero", {}).get("image_url") and not force_regenerate:
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Images already generated. Skipped.",
                "latest_package_key": LATEST_PACKAGE_KEY,
            }),
        }

    run_id = latest_wrapper["run_id"]
    generated_at = latest_wrapper["generated_at"]

    yyyy = generated_at[0:4]
    mm = generated_at[5:7]
    dd = generated_at[8:10]

    archive_package_key = get_archive_package_key(latest_wrapper)

    latest_image_prefix = "published/news-site/latest/images"
    archive_image_prefix = f"published/news-site/archive/{yyyy}/{mm}/{dd}/images/{run_id}"

    latest_inner_for_write = deep_copy(latest_inner)
    archive_inner_for_write = deep_copy(latest_inner)

    jobs = build_jobs(latest_inner)
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

        latest_key = f"{latest_image_prefix}/{job['filename']}"
        archive_key = f"{archive_image_prefix}/{job['filename']}"

        s3.put_object(
            Bucket=BUCKET,
            Key=latest_key,
            Body=image_bytes,
            ContentType="image/webp",
        )

        s3.put_object(
            Bucket=BUCKET,
            Key=archive_key,
            Body=image_bytes,
            ContentType="image/webp",
        )

        apply_image_url(latest_inner_for_write, job, "/" + latest_key)
        apply_image_url(archive_inner_for_write, job, "/" + archive_key)

        images_written.append({
            "name": job["name"],
            "latest_key": latest_key,
            "archive_key": archive_key,
        })

    latest_wrapper_for_write = deep_copy(latest_wrapper)
    archive_wrapper_for_write = deep_copy(latest_wrapper)

    set_inner_package(latest_wrapper_for_write, latest_inner_for_write)
    set_inner_package(archive_wrapper_for_write, archive_inner_for_write)

    write_json_s3(LATEST_PACKAGE_KEY, latest_wrapper_for_write)
    write_json_s3(archive_package_key, archive_wrapper_for_write)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "SkyGrid news-site images generated",
            "model": f"{MODEL_OWNER}/{MODEL_NAME}",
            "latest_package_key": LATEST_PACKAGE_KEY,
            "archive_package_key": archive_package_key,
            "images_written": images_written,
        }),
    }