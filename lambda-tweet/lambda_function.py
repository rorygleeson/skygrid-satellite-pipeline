import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import boto3
import requests
from botocore.exceptions import ClientError
from requests_oauthlib import OAuth1


BUCKET = os.environ.get("SKYGRID_BUCKET", "skygrid.io")
STATE_KEY = os.environ.get("X_STATE_KEY", "published/x/state.json")
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://skygrid.io")
X_SECRET_NAME_ENV = "X_SECRET_NAME"

MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")
POST_SEQUENCE = ["hero", "story_1", "story_2", "story_3", "story_4", "story_5"]
NEXT_ITEM = {
    "hero": "story_1",
    "story_1": "story_2",
    "story_2": "story_3",
    "story_3": "story_4",
    "story_4": "story_5",
    "story_5": "done",
}

secrets_client = boto3.client("secretsmanager")
s3 = boto3.client("s3")


def today_melbourne_date() -> str:
    return datetime.now(timezone.utc).astimezone(MELBOURNE_TZ).strftime("%Y-%m-%d")


def archive_package_key(date_text: str) -> str:
    yyyy, mm, dd = date_text.split("-")
    return f"published/news-site/archive/{yyyy}/{mm}/{dd}/package.json"


def report_url(date_text: str) -> str:
    return f"{SITE_BASE_URL.rstrip('/')}/news.html?date={date_text}"


def read_json_s3(key: str):
    response = s3.get_object(Bucket=BUCKET, Key=key)
    return json.loads(response["Body"].read().decode("utf-8"))


def write_json_s3(key: str, data) -> None:
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )


def read_state_or_none():
    try:
        return read_json_s3(STATE_KEY)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ["NoSuchKey", "404"]:
            return None
        raise


def initial_state(date_text: str) -> dict:
    return {
        "date": date_text,
        "next_item": "hero",
    }


def get_effective_state(date_text: str) -> dict:
    state = read_state_or_none()

    if not state:
        return initial_state(date_text)

    if state.get("date") != date_text:
        return initial_state(date_text)

    if state.get("next_item") not in POST_SEQUENCE + ["done"]:
        return initial_state(date_text)

    return state


def strip_code_fence(text: str) -> str:
    return (
        text.replace("```json", "")
        .replace("```", "")
        .strip()
    )


def unwrap_news_package(wrapper: dict) -> dict:
    text = wrapper.get("response", {}).get("result", {}).get("content", [{}])[0].get("text")
    if not text:
        return wrapper
    return json.loads(strip_code_fence(text))


def get_x_credentials() -> dict:
    secret_name = os.environ[X_SECRET_NAME_ENV]
    response = secrets_client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def get_auth(creds: dict) -> OAuth1:
    return OAuth1(
        creds["consumer_key"],
        creds["consumer_secret"],
        creds["access_token"],
        creds["access_token_secret"],
    )


def select_story(inner_package: dict, item_name: str) -> dict:
    if item_name == "hero":
        return inner_package["hero"]

    if item_name.startswith("story_"):
        story_number = int(item_name.split("_")[1])
        for story in inner_package.get("stories", []):
            if int(story.get("story_number", -1)) == story_number:
                return story

    raise RuntimeError(f"Could not find item {item_name} in package")


def parse_send_link(event: dict) -> bool:
    value = event.get("sendLink", True)

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value.strip().lower() not in ["false", "0", "no", "n"]

    return bool(value)


def build_tweet_text(story: dict, date_text: str, send_link: bool = True) -> str:
    headline = (story.get("headline") or "SkyGrid Daily News").strip()
    summary = (story.get("short_summary") or "").strip()
    link = report_url(date_text)

    if send_link:
        footer = f"Read today's full report:\n{link}\n\n#Starlink"
    else:
        footer = "#Starlink #SkyGrid"

    if summary:
        text = f"{headline}\n\n{summary}\n\n{footer}"
    else:
        text = f"{headline}\n\n{footer}"

    if len(text) <= 280:
        return text

    # Trim only the short summary. Preserve headline and footer.
    fixed = f"{headline}\n\n\n\n{footer}"
    max_summary_length = max(0, 280 - len(fixed) - 1)

    if max_summary_length <= 0:
        # Extremely long headline case. Last resort: trim headline but keep link.
        max_headline_length = max(20, 280 - len(f"\n\n{footer}") - 1)
        trimmed_headline = headline[:max_headline_length].rstrip() + "…"
        return f"{trimmed_headline}\n\n{footer}"[:280]

    trimmed_summary = summary[:max_summary_length].rstrip() + "…"
    return f"{headline}\n\n{trimmed_summary}\n\n{footer}"[:280]


def image_url_to_s3_key(image_url: str) -> str:
    if not image_url:
        raise RuntimeError("Story has no image_url")

    key = image_url.split("?", 1)[0].strip()
    if key.startswith("https://skygrid.io/"):
        key = key.replace("https://skygrid.io/", "", 1)
    elif key.startswith(f"{SITE_BASE_URL.rstrip('/')}/"):
        key = key.replace(f"{SITE_BASE_URL.rstrip('/')}/", "", 1)
    elif key.startswith("/"):
        key = key[1:]

    if not key:
        raise RuntimeError(f"Could not convert image_url to S3 key: {image_url}")

    return key


def read_image_bytes_from_s3(image_url: str) -> tuple[bytes, str]:
    key = image_url_to_s3_key(image_url)
    response = s3.get_object(Bucket=BUCKET, Key=key)
    content_type = response.get("ContentType") or "image/webp"
    return response["Body"].read(), content_type


def upload_image_to_x(auth: OAuth1, image_bytes: bytes, content_type: str) -> str:
    # OAuth 1.0a simple image upload. This keeps using the same keys that already
    # work for your text-only Lambda.
    files = {
        "media": ("skygrid.webp", image_bytes, content_type),
    }
    data = {
        "media_category": "tweet_image",
    }

    response = requests.post(
        "https://upload.twitter.com/1.1/media/upload.json",
        auth=auth,
        files=files,
        data=data,
        timeout=60,
    )

    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text}

    if response.status_code >= 300:
        raise Exception(json.dumps({
            "stage": "upload_image_to_x",
            "status_code": response.status_code,
            "response": body,
        }))

    media_id = body.get("media_id_string") or str(body.get("media_id", ""))
    if not media_id:
        raise RuntimeError(f"X media upload did not return media_id: {body}")

    return media_id


def post_tweet(auth: OAuth1, text: str, media_id: str | None = None) -> dict:
    payload = {"text": text}

    if media_id:
        payload["media"] = {"media_ids": [media_id]}

    response = requests.post(
        "https://api.x.com/2/tweets",
        auth=auth,
        json=payload,
        timeout=15,
    )

    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text}

    if response.status_code >= 300:
        raise Exception(json.dumps({
            "stage": "post_tweet",
            "status_code": response.status_code,
            "response": body,
        }))

    return body


def lambda_handler(event, context):
    event = event or {}

    # Empty event is normal production behavior.
    # Optional date is only for manual testing/backfill.
    date_text = event.get("date") or today_melbourne_date()

    if not isinstance(date_text, str) or not date_text or len(date_text) != 10:
        raise ValueError("date must be YYYY-MM-DD if supplied")

    dry_run = bool(event.get("dry_run", False))
    force_item = event.get("force_item")  # optional manual test only
    send_link = parse_send_link(event)

    state = get_effective_state(date_text)
    item_to_post = force_item or state["next_item"]

    if item_to_post == "done":
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "No tweet sent. Daily sequence already complete.",
                "date": date_text,
                "state": state,
            }),
        }

    if item_to_post not in POST_SEQUENCE:
        raise ValueError(f"Invalid next item: {item_to_post}")

    package_key = archive_package_key(date_text)
    wrapper = read_json_s3(package_key)
    inner = unwrap_news_package(wrapper)
    story = select_story(inner, item_to_post)

    tweet_text = build_tweet_text(story, date_text, send_link)
    image_url = story.get("image_url")

    if not image_url:
        raise RuntimeError(f"{item_to_post} has no image_url. Image Lambda may not have completed.")

    if dry_run:
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Dry run only. No tweet sent and state not advanced.",
                "date": date_text,
                "package_key": package_key,
                "item": item_to_post,
                "tweet_text": tweet_text,
                "tweet_length": len(tweet_text),
                "sendLink": send_link,
                "image_url": image_url,
                "current_state": state,
            }, ensure_ascii=False),
        }

    creds = get_x_credentials()
    auth = get_auth(creds)

    image_bytes, content_type = read_image_bytes_from_s3(image_url)
    media_id = upload_image_to_x(auth, image_bytes, content_type)
    tweet_response = post_tweet(auth, tweet_text, media_id)

    next_item = NEXT_ITEM[item_to_post]
    updated_state = {
        "date": date_text,
        "next_item": next_item,
        "last_item_posted": item_to_post,
        "last_x_post_id": tweet_response.get("data", {}).get("id"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # State advances only after image upload and tweet creation succeeded.
    if not force_item:
        write_json_s3(STATE_KEY, updated_state)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "SkyGrid X post sent",
            "date": date_text,
            "package_key": package_key,
            "item_posted": item_to_post,
            "next_item": next_item,
            "tweet_length": len(tweet_text),
            "sendLink": send_link,
            "image_url": image_url,
            "media_id": media_id,
            "x_response": tweet_response,
            "state_updated": not bool(force_item),
        }, ensure_ascii=False),
    }
