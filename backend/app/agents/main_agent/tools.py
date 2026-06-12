"""
tools.py
────────
Tools available to the main agent's information-gathering subagents.

``web_search`` wraps DDGS (DuckDuckGo) for text / news / image / video search.
The search backend is fixed by the developer; the agent only chooses the
source type and query.
"""

from typing import Literal

from ddgs import DDGS
from langchain_core.tools import tool

from app.agents.shared.workspace_paths import resolve_in_thread
from app.logger import get_logger

logger = get_logger(__name__)

# Developer-fixed search engine (agent never selects the backend).
SEARCH_ENGINE = "duckduckgo"

Region = Literal[
    "wt-wt", "us-en", "uk-en", "in-en", "de-de",
    "fr-fr", "es-es", "it-it", "nl-nl", "jp-jp",
]
SafeSearch = Literal["on", "moderate", "off"]
TimeLimit = Literal["d", "w", "m", "y"]
Source = Literal["text", "news", "images", "videos"]


@tool
def web_search(
    query: str,
    source: Source = "text",
    region: Region = "wt-wt",
    safesearch: SafeSearch = "moderate",
    timelimit: TimeLimit | None = None,
    max_results: int = 10,
) -> dict:
    """
    Search the web. Supports text, news, images and videos.

    Returns a normalized dict with a ``results`` list. Use this to gather
    up-to-date information from the internet.
    """
    try:
        with DDGS() as ddgs:
            if source == "text":
                results = list(
                    ddgs.text(
                        query=query,
                        region=region,
                        safesearch=safesearch,
                        timelimit=timelimit,
                        backend=SEARCH_ENGINE,
                        max_results=max_results,
                    )
                )
            elif source == "news":
                results = list(
                    ddgs.news(
                        query=query,
                        region=region,
                        safesearch=safesearch,
                        timelimit=timelimit,
                        max_results=max_results,
                    )
                )
            elif source == "images":
                results = list(
                    ddgs.images(
                        query=query,
                        region=region,
                        safesearch=safesearch,
                        max_results=max_results,
                    )
                )
            elif source == "videos":
                results = list(
                    ddgs.videos(
                        query=query,
                        region=region,
                        safesearch=safesearch,
                        timelimit=timelimit,
                        max_results=max_results,
                    )
                )
            else:
                raise ValueError(f"Unsupported source: {source}")

        normalized = []
        for r in results:
            if source == "images":
                normalized.append(
                    {
                        "title": r.get("title"),
                        "image_url": r.get("image"),
                        "thumbnail": r.get("thumbnail"),
                        "source_url": r.get("url"),
                        "source": r.get("source"),
                    }
                )
            elif source == "videos":
                normalized.append(
                    {
                        "title": r.get("title"),
                        "url": r.get("content") or r.get("url"),
                        "description": r.get("description"),
                        "duration": r.get("duration"),
                        "published": r.get("published"),
                    }
                )
            else:
                normalized.append(
                    {
                        "title": r.get("title"),
                        "url": r.get("href") or r.get("url"),
                        "snippet": r.get("body"),
                    }
                )

        return {
            "query": query,
            "source": source,
            "engine": SEARCH_ENGINE,
            "count": len(normalized),
            "results": normalized,
        }

    except Exception as e:
        logger.exception("web_search failed")
        return {"query": query, "source": source, "error": str(e)}


logger.info("Successfully created `web_search` tool.")


@tool
def delete_file(paths: list[str]) -> dict:
    """
    Permanently delete one or more files from the user's current workspace
    (the active conversation's thread directory).

    This is a destructive action and is gated by human approval. Pass the same
    paths you use with the filesystem tools, e.g. ["main.py"] or
    ["/workspace/reports/old.md"]. Directories are NOT deleted.

    Args:
        paths: File paths to delete, relative to the workspace root.

    Returns:
        A dict reporting which files were deleted, skipped, or not found.
    """
    deleted: list[str] = []
    not_found: list[str] = []
    skipped: list[dict] = []

    for raw in paths or []:
        try:
            target = resolve_in_thread(raw)
        except ValueError as exc:
            skipped.append({"path": raw, "reason": str(exc)})
            continue

        if not target.exists():
            not_found.append(raw)
            continue
        if target.is_dir():
            skipped.append({"path": raw, "reason": "is a directory; not deleted"})
            continue
        try:
            target.unlink()
            deleted.append(raw)
        except Exception as exc:  # pragma: no cover - filesystem error path
            skipped.append({"path": raw, "reason": str(exc)})

    logger.info("delete_file: deleted=%s not_found=%s skipped=%s", deleted, not_found, skipped)
    return {"deleted": deleted, "not_found": not_found, "skipped": skipped}


logger.info("Successfully created `delete_file` tool.")

# The main agent exposes the destructive `delete_file` tool (HITL-gated). Its
# read/write/search/run capabilities come from middleware + subagents.
tools: list = [delete_file]

