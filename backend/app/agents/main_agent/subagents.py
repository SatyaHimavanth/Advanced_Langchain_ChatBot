"""
subagents.py
────────────
Information-gathering subagents delegated to by the main agent via the
``task`` tool (SubAgentMiddleware). Each subagent runs in an isolated context
and returns a consolidated report, keeping the main agent's context clean.

  • websearch  — searches the live web (text / news / images / videos).
  • weather    — looks up current conditions and a short forecast for a city.
  • explorer   — reads and summarizes documents in the user's /workspace/ and
                 cross-references the web; returns a consolidated digest.
"""

from langchain.chat_models import BaseChatModel

from app.agents.main_agent.tools import web_search
from app.agents.weather_agent.tools import get_weather


def create_subagents(*, llm: BaseChatModel) -> list[dict]:
    """Build the list of subagent specs for SubAgentMiddleware."""
    return [
        {
            "name": "websearch",
            "description": (
                "Delegate live web research here. Give a focused query and the "
                "subagent returns a concise, sourced summary of findings."
            ),
            "system_prompt": (
                "You are a web research specialist. Use the web_search tool to "
                "gather information, then return a concise summary with the key "
                "facts and their source URLs. Do not pad the answer."
            ),
            "tools": [web_search],
            "model": llm,
            "middleware": [],
        },
        {
            "name": "weather",
            "description": (
                "Delegate weather lookups here. Provide a city and the subagent "
                "returns current conditions and a short forecast."
            ),
            "system_prompt": (
                "You are a weather specialist. Use the get_weather tool and "
                "return a short, friendly weather summary for the requested city."
            ),
            "tools": [get_weather],
            "model": llm,
            "middleware": [],
        },
        {
            "name": "explorer",
            "description": (
                "Delegate document exploration here. The subagent reads files in "
                "the workspace, optionally cross-references the web, and returns "
                "a consolidated digest of what it found."
            ),
            "system_prompt": (
                "You are a document explorer. Use the filesystem tools (ls, "
                "read_file, glob, grep) with plain relative filenames to inspect "
                "files in the workspace, and web_search when external context is "
                "needed. Return a single consolidated digest of the relevant "
                "information — do not dump raw file contents."
            ),
            "tools": [web_search],
            "model": llm,
            "middleware": [],
        },
    ]
