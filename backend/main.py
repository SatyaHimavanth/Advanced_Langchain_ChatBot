"""
Entry point for the Advanced LangChain ChatBot backend.

Run with:   python main.py     (or: uv run main.py)

On Windows the asyncio event-loop MUST be a SelectorEventLoop, because
psycopg's async mode is incompatible with the default ProactorEventLoop.
We set the policy AND drive uvicorn's server coroutine inside a loop we create
ourselves (via asyncio.run), so uvicorn cannot fall back to a Proactor loop.
"""

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def run() -> None:
    import uvicorn

    config = uvicorn.Config(
        "app.server:app",
        host="0.0.0.0",
        port=8000,
        # Use plain asyncio (not uvloop/auto) so the loop we create below is used.
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    # asyncio.run() builds a fresh loop from the policy set above (Selector on
    # Windows) and runs uvicorn inside it.
    asyncio.run(server.serve())


if __name__ == "__main__":
    run()