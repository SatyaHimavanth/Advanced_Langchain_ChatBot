from uuid import uuid4

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agents.coding_agent.agent import (
    coding_agent,
)
from agents.utils.streaming import (
    InterruptAction,
    build_agent_input,
    stream_agent_sse,
)
from agent_contexts import Context


app = FastAPI(
    title="LangChain Chat API",
    version="1.0.0",
)


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# Request Model
# ============================================================

class ChatRequest(BaseModel):

    thread_id: str | None = None

    user_name: str = "Anonymous"

    message: str | None = None

    interrupt_action: (
        InterruptAction | None
    ) = None


# ============================================================
# Health
# ============================================================

@app.get("/health")
async def health():
    return {"status": "ok"}


# ============================================================
# Chat
# ============================================================

@app.post("/chat")
async def chat(
    request: ChatRequest,
):

    thread_id = (
        request.thread_id
        or str(uuid4())
    )

    config = {
        "configurable": {
            "thread_id": thread_id,
        }
    }

    context = Context(
        user_name=request.user_name,
    )

    agent_input = build_agent_input(
        message=request.message,
        interrupt_action=request.interrupt_action,
    )

    return StreamingResponse(
        stream_agent_sse(
            agent=coding_agent,
            input=agent_input,
            config=config,
            context=context,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ============================================================
# Run
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
    )