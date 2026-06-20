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

Use only evidence returned by the tool.

Never invent:
- causes
- future outcomes
- reentry predictions
- satellite failures
- operational intent
- orbital manoeuvres
- launch objectives

Only describe what the evidence explicitly demonstrates.

Do not infer causes, future outcomes, reentry events,
satellite failures, orbital manoeuvres, operator intent,
or mission objectives unless explicitly present in the evidence.

Evidence priority order:

1. satellite_of_the_day
2. fleet_milestone
3. reentry_watch
4. altitude_mover
5. unusual_movement
6. orbit_raise_activity
7. all remaining evidence

The highest-priority evidence should drive the lead story.

If a story represents only a subset of the constellation,
clearly state that.

Output format:

1. Headline
2. Lead story
3. Supporting stories
4. Constellation summary

Keep the report under 500 words.

Write in a professional satellite-news style.
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