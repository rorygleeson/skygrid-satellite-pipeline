import json
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from zoneinfo import ZoneInfo

AGENT_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:134387458447:runtime/skygrid1-3nYW137TZj"
)
BUCKET = "skygrid.io"


BOTO_CONFIG = Config(connect_timeout=10, read_timeout=420, retries={"max_attempts": 1})

agentcore = boto3.client(
    "bedrock-agentcore", region_name="us-east-1", config=BOTO_CONFIG
)

s3 = boto3.client("s3", config=BOTO_CONFIG)


def invoke_agent(prompt: str, session_name: str) -> dict:
    now = datetime.now(timezone.utc)
    session_id = f"skygrid-publisher-{session_name}-{now.strftime('%Y%m%dT%H%M%SZ')}"

    response = agentcore.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": prompt}),
    )

    raw_body = response["response"].read().decode("utf-8")

    try:
        return json.loads(raw_body)
    except json.JSONDecodeError:
        return {"raw_response": raw_body}


def write_json(key: str, data) -> None:
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )


def read_json(key: str, default):
    try:
        response = s3.get_object(Bucket=BUCKET, Key=key)
        return json.loads(response["Body"].read().decode("utf-8"))
    except ClientError as e:
        if e.response["Error"]["Code"] in ["NoSuchKey", "404"]:
            return default
        raise


def update_news_site_archive_index(run_id: str, archive_key: str, now: datetime) -> str:
    index_key = "published/news-site/archive/index.json"
    melbourne_now = now.astimezone(ZoneInfo("Australia/Melbourne"))
    archive_index = read_json(index_key, [])

    entry = {
        "label": melbourne_now.strftime("%d %b %Y"),
        "date": melbourne_now.strftime("%Y-%m-%d"),
        "run_id": run_id,
        "package_key": archive_key,
    }

    archive_index = [
        item for item in archive_index if item.get("date") != entry["date"]
    ]

    archive_index.insert(0, entry)

    archive_index = archive_index[:60]

    write_json(index_key, archive_index)

    return index_key


def lambda_handler(event, context):
    now = datetime.now(timezone.utc)

    yyyy = now.strftime("%Y")
    mm = now.strftime("%m")
    dd = now.strftime("%d")
    run_id = now.strftime("%Y%m%dT%H%M%SZ")

    daily_prompt = """
Generate today's SkyGrid daily satellite news report.

Use only today's Athena evidence.

Return a professional news reporter style report.

Do not invent causes, future outcomes, reentry predictions,
failures, manoeuvres, launch objectives, or operator intent.
"""

    tiktok_prompt = """
Create exactly 5 SkyGrid TikTok image cards based only on today's Athena evidence.

Return ONLY raw valid JSON.
Do not include markdown.
Do not wrap the response in ```json.
Do not wrap the response in code fences.
Do not include explanation outside the JSON.

Use this exact structure:
{
  "type": "tiktok_image_package",
  "cards": [
    {
      "card_number": 1,
      "headline": "...",
      "big_number": "...",
      "short_caption": "...",
      "image_prompt": "...",
      "evidence_note": "..."
    }
  ]
}

Rules:
- Create exactly 5 cards.
- Use evidence only.
- Do not invent causes, future outcomes, reentry predictions, failures, manoeuvres, launch objectives, or operator intent.
- Prefer phrases like "reentry watch band" instead of "danger zone" or "imminent reentry".
"""

    news_site_prompt = """
Generate a SkyGrid daily website news package based only on today's Athena evidence.

Return ONLY raw valid JSON.
Do not include markdown.
Do not wrap the response in ```json.
Do not wrap the response in code fences.
Do not include explanation outside the JSON.

Create:
- one hero story
- exactly five concise website news stories

Return this exact JSON structure:
{
  "type": "daily_news_site_package",
  "title": "...",
  "subtitle": "...",
  "hero": {
    "headline": "...",
    "big_number": "...",
    "short_summary": "...",
    "detailed_body": "...",
    "image_prompt": "...",
    "evidence_note": "..."
  },
  "stories": [
    {
      "story_number": 1,
      "headline": "...",
      "big_number": "...",
      "short_summary": "...",
      "detailed_body": "...",
      "image_prompt": "...",
      "evidence_note": "..."
    }
  ]
}

Rules:
 • Create exactly 5 stories in the stories array.
 • The hero story may overlap with story 1, but it should be written as the page lead.
 • detailed_body should be 100 to 140 words per story.
 • Each detailed_body should:
  - explain what happened
  - explain why it matters
  - provide relevant scientific or orbital context
  - explain operational significance where appropriate
 • Avoid simply restating evidence.
 • short_summary should be one sentence.
 • image_prompt should describe a single compelling image suitable for AI image generation.
 • image_prompt should be 1 to 3 sentences only.
 • image_prompt should focus on visual elements only.
 • image_prompt must not contain article text, headlines, captions, evidence notes, measurements, labels, telemetry, branding instructions, dashboard elements, charts, graphs, or infographic layouts.
 • Use a consistent SkyGrid visual identity: premium space journalism aesthetic, realistic satellite imagery, blue/cyan accent colours, cinematic lighting, high contrast, futuristic aerospace visual design.
 • The image composition, scene, camera angle, environment, and visual elements should be driven primarily by the story being illustrated.
 • Do not request text overlays, data overlays, telemetry overlays, numeric overlays, labels, captions, branding, logos, watermarks, dashboards, charts, graphs, HUDs, UI elements, or infographic elements.
 • The generated image should resemble a magazine-quality aerospace photograph or cinematic space scene rather than an infographic.
 
 Writing Style:

• Write like a professional aerospace news publication.
• Make stories informative, scientific, and engaging.
• Use precise language rather than sensational language.
• Explain the significance of observations.
• Where appropriate, discuss atmospheric drag, orbital altitude,
  constellation behaviour, orbit-raising activity,
  reentry-watch conditions, and orbital mechanics concepts.

• General scientific explanations are permitted.
• Specific causes are not permitted unless present in evidence.

Avoid:
• database-style wording
• query-style wording
• evidence dump language
• repetitive sentence structures
• phrases such as:
  - selection_scope
  - query result
  - priority score
  - evidence demonstrates
 
 • Use evidence only.
 • Do not invent causes, future outcomes, reentry predictions, satellite failures, manoeuvres, launch objectives, or operator intent.
 • Prefer "reentry watch band" over "danger zone" or "imminent reentry".
 • If evidence is a top-N subset, clearly state that in detailed_body or evidence_note.
 • Do not include any calendar date in the title or subtitle.
 • The subtitle must be a short topic-only phrase.
 • The subtitle must not contain a day, month, year, or date-like text.
 • Bad example: "19 June 2026 – Tracking 10,618 Starlink Satellites"
 • Good example: "Tracking 10,618 Starlink Satellites"
 • Report dates are supplied by the publisher wrapper generated_at field.
"""

    daily_response = invoke_agent(daily_prompt, "daily-report")
    tiktok_response = invoke_agent(tiktok_prompt, "tiktok-package")
    news_site_response = invoke_agent(news_site_prompt, "news-site-package")

    daily_output = {
        "type": "daily_report",
        "run_id": run_id,
        "generated_at": now.isoformat(),
        "agent_runtime_arn": AGENT_RUNTIME_ARN,
        "response": daily_response,
    }

    tiktok_output = {
        "type": "tiktok_package",
        "run_id": run_id,
        "generated_at": now.isoformat(),
        "agent_runtime_arn": AGENT_RUNTIME_ARN,
        "response": tiktok_response,
    }

    news_site_output = {
        "type": "news_site_package",
        "run_id": run_id,
        "generated_at": now.isoformat(),
        "agent_runtime_arn": AGENT_RUNTIME_ARN,
        "response": news_site_response,
    }

    daily_latest_key = "published/news/latest/daily_report.json"
    daily_archive_key = (
        f"published/news/archive/{yyyy}/{mm}/{dd}/daily_report.json"
    )

    tiktok_latest_key = "published/tiktok/latest/tiktok_package.json"
    tiktok_archive_key = (
        f"published/tiktok/archive/{yyyy}/{mm}/{dd}/tiktok_package.json"
    )

    news_site_latest_key = "published/news-site/latest/package.json"
    news_site_archive_key = (
        f"published/news-site/archive/{yyyy}/{mm}/{dd}/package.json"
    )

    write_json(daily_latest_key, daily_output)
    write_json(daily_archive_key, daily_output)

    write_json(tiktok_latest_key, tiktok_output)
    write_json(tiktok_archive_key, tiktok_output)

    write_json(news_site_latest_key, news_site_output)
    write_json(news_site_archive_key, news_site_output)

    news_site_archive_index_key = update_news_site_archive_index(
        run_id=run_id,
        archive_key=news_site_archive_key,
        now=now,
    )

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": "SkyGrid publishing complete",
                "daily_latest": daily_latest_key,
                "daily_archive": daily_archive_key,
                "tiktok_latest": tiktok_latest_key,
                "tiktok_archive": tiktok_archive_key,
                "news_site_latest": news_site_latest_key,
                "news_site_archive": news_site_archive_key,
                "news_site_archive_index": news_site_archive_index_key,
            }
        ),
    }
