import logging
from typing import Literal

from ddgs import DDGS
from langchain_core.tools import tool
from logger import get_logger


logger = get_logger(__name__)

# Developer Configuration for search engine
search_engines: Literal[
    "auto",
    "bing",
    "brave",
    "duckduckgo",
    "google",
    "grokipedia",
    "mojeek",
    "startpage",
    "yandex",
    "yahoo",
    "wikipedia",
] = "duckduckgo"

DEFAULT_TEXT_ENGINE = search_engines
DEFAULT_NEWS_ENGINE = search_engines
DEFAULT_IMAGE_ENGINE = search_engines
DEFAULT_VIDEO_ENGINE = search_engines


# Types
Region = Literal[
    "wt-wt",
    "us-en",
    "uk-en",
    "in-en",
    "de-de",
    "fr-fr",
    "es-es",
    "it-it",
    "nl-nl",
    "jp-jp",
]

SafeSearch = Literal[
    "on",
    "moderate",
    "off",
]

TimeLimit = Literal[
    "d",
    "w",
    "m",
    "y",
]

Source = Literal[
    "text",
    "news",
    "images",
    "videos",
]


# Backend Resolver
def _get_backend(source: Source) -> str:
    """
    Fixed search engines controlled by developer.
    Agent never selects backend.
    """

    return {
        "text": DEFAULT_TEXT_ENGINE,
        "news": DEFAULT_NEWS_ENGINE,
        "images": DEFAULT_IMAGE_ENGINE,
        "videos": DEFAULT_VIDEO_ENGINE,
    }[source]


# Tool
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
    Search the web.

    Supports:
    - text
    - news
    - images
    - videos
    """

    try:

        backend = _get_backend(source)

        with DDGS() as ddgs:

            if source == "text":

                results = list(
                    ddgs.text(
                        query=query,
                        region=region,
                        safesearch=safesearch,
                        timelimit=timelimit,
                        backend=backend,
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
                raise ValueError(
                    f"Unsupported source: {source}"
                )

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
                        "url": r.get("content")
                        or r.get("url"),
                        "description": r.get("description"),
                        "duration": r.get("duration"),
                        "published": r.get("published"),
                    }
                )

            else:

                normalized.append(
                    {
                        "title": r.get("title"),
                        "url": r.get("href")
                        or r.get("url"),
                        "snippet": r.get("body"),
                    }
                )

        return {
            "query": query,
            "source": source,
            "engine": backend,
            "count": len(normalized),
            "results": normalized,
        }

    except Exception as e:

        return {
            "query": query,
            "source": source,
            "error": str(e),
        }
logger.info("Successfully created `web_search` tool.")

logger.info("Grouping main agent tools...")
tools = []
logger.info("Main agent tools are successfully created.")

if __name__ == "__main__":

    tests = [
        {
            "name": "Text Search - Recent LangGraph Content",
            "params": {
                "query": "LangGraph multi agent architecture",
                "source": "text",
                "timelimit": "m",
                "max_results": 5,
            },
        },
        {
            "name": "News Search - GPT-5",
            "params": {
                "query": "OpenAI GPT-5",
                "source": "news",
                "timelimit": "w",
                "max_results": 5,
            },
        },
        {
            "name": "Image Search - Golden Retriever",
            "params": {
                "query": "Golden Retriever puppy",
                "source": "images",
                "safesearch": "on",
                "max_results": 5,
            },
        },
        {
            "name": "Video Search - LangChain Tutorial",
            "params": {
                "query": "LangChain tutorial",
                "source": "videos",
                "timelimit": "y",
                "max_results": 5,
            },
        },
        {
            "name": "Regional Search - India AI Regulations",
            "params": {
                "query": "artificial intelligence regulations",
                "source": "text",
                "region": "in-en",
                "timelimit": "y",
                "max_results": 10,
            },
        },
    ]

    for idx, test in enumerate(tests, start=1):

        print("\n" + "=" * 80)
        print(f"TEST {idx}: {test['name']}")
        print("=" * 80)

        result = web_search.invoke(test["params"])

        if "error" in result:
            print("FAILED")
            print(result["error"])
            continue

        print(f"Query   : {result['query']}")
        print(f"Source  : {result['source']}")
        print(f"Engine  : {result['engine']}")
        print(f"Results : {result['count']}")

        print("\nTop Results:\n")

        for i, item in enumerate(result["results"][:3], start=1):

            print(f"[{i}]")

            if result["source"] == "images":
                print("Title      :", item.get("title"))
                print("Image URL  :", item.get("image_url"))
                print("Source URL :", item.get("source_url"))

            elif result["source"] == "videos":
                print("Title       :", item.get("title"))
                print("URL         :", item.get("url"))
                print("Duration    :", item.get("duration"))
                print("Published   :", item.get("published"))

            else:
                print("Title   :", item.get("title"))
                print("URL     :", item.get("url"))
                print("Snippet :", item.get("snippet"))

            print("-" * 40)