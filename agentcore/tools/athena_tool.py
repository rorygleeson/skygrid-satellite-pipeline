import json
import time
import boto3

ATHENA_DATABASE = "skygrid_analytics"
ATHENA_WORKGROUP = "primary"
ATHENA_OUTPUT = "s3://skygrid.io/athena-results/"

athena = boto3.client(
    "athena",
    region_name="us-east-1"
)


def get_daily_story_candidates() -> str:

    query = """
    SELECT
      story_type,
      priority,
      selection_scope,
      population_size,
      evidence_scope,
      object_name,
      norad_cat_id,
      headline_hint,
      evidence_detail
    FROM vw_daily_story_candidates
    ORDER BY priority DESC, story_type, object_name
    """

    response = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={
            "Database": ATHENA_DATABASE
        },
        ResultConfiguration={
            "OutputLocation": ATHENA_OUTPUT
        },
        WorkGroup=ATHENA_WORKGROUP,
    )

    query_id = response["QueryExecutionId"]

    while True:

        status = athena.get_query_execution(
            QueryExecutionId=query_id
        )

        state = status["QueryExecution"]["Status"]["State"]

        if state == "SUCCEEDED":
            break

        if state in ["FAILED", "CANCELLED"]:

            reason = status["QueryExecution"]["Status"].get(
                "StateChangeReason",
                "Unknown"
            )

            raise RuntimeError(
                f"Athena query failed: {reason}"
            )

        time.sleep(1)

    results = athena.get_query_results(
        QueryExecutionId=query_id
    )

    rows = results["ResultSet"]["Rows"]

    headers = [
        c.get("VarCharValue", "")
        for c in rows[0]["Data"]
    ]

    evidence = []

    for row in rows[1:]:

        values = [
            cell.get("VarCharValue", "")
            for cell in row.get("Data", [])
        ]

        evidence.append(
            dict(zip(headers, values))
        )

    return json.dumps(
        {
            "source_view": "vw_daily_story_candidates",
            "row_count": len(evidence),
            "evidence": evidence
        },
        indent=2
    )