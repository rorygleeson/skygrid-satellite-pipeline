from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent, tool

from tools.athena_tool import get_daily_story_candidates

app = BedrockAgentCoreApp()


@tool
def fetch_story_evidence() -> str:
    """
    Retrieve SkyGrid daily story evidence from Athena.
    """
    return get_daily_story_candidates()


SYSTEM_PROMPT = """
You are the SkyGrid Daily Reporter.

You must always call fetch_story_evidence first.

Use only evidence returned from the tool.

Do not invent facts.

Respect:
- selection_scope
- evidence_scope
- population_size

If evidence represents a top-N subset,
make that clear in the report.

Write a concise professional satellite news report.
"""


@app.entrypoint
def invoke(payload):

    user_message = payload.get(
        "prompt",
        "Generate today's SkyGrid satellite news report."
    )

    agent = Agent(
        model="global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        tools=[fetch_story_evidence],
        system_prompt=SYSTEM_PROMPT
    )

    result = agent(user_message)

    return {
        "result": result.message
    }


if __name__ == "__main__":
    app.run()