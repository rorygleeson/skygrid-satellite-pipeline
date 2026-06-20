import json
from datetime import datetime, timezone

import boto3

AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:134387458447:runtime/skygrid1-3nYW137TZj"
BUCKET = "skygrid.io"

agentcore = boto3.client("bedrock-agentcore", region_name="us-east-1")
s3 = boto3.client("s3")


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
        return {
            "raw_response": raw_body
        }


def write_json(key: str, data: dict) -> None:
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )


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

    daily_response = invoke_agent(daily_prompt, "daily-report")
    tiktok_response = invoke_agent(tiktok_prompt, "tiktok-package")

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

    daily_latest_key = "published/news/latest/daily_report.json"
    daily_archive_key = f"published/news/archive/{yyyy}/{mm}/{dd}/daily_report_{run_id}.json"

    tiktok_latest_key = "published/tiktok/latest/tiktok_package.json"
    tiktok_archive_key = f"published/tiktok/archive/{yyyy}/{mm}/{dd}/tiktok_package_{run_id}.json"

    write_json(daily_latest_key, daily_output)
    write_json(daily_archive_key, daily_output)

    write_json(tiktok_latest_key, tiktok_output)
    write_json(tiktok_archive_key, tiktok_output)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "SkyGrid publishing complete",
            "daily_latest": daily_latest_key,
            "daily_archive": daily_archive_key,
            "tiktok_latest": tiktok_latest_key,
            "tiktok_archive": tiktok_archive_key,
        }),
    }