from uuid import uuid4

from langchain.messages import HumanMessage

from agents.shared.agent_contexts import Context
from agents.supervisor_agent.agent import create_supervisor_agent


async def check_agent():
    user_input = (
        "Write a python code to find out the years passed since 1 Jan 2000 "
        "until current date. Then save it to workspace as date_finder.py "
        "and verify its creation."
    )

    messages = {
        "messages": [
            HumanMessage(content=user_input)
        ]
    }

    config = {
        "configurable": {
            "thread_id": str(uuid4()),
            "user_id": "demo-user",
            "tenant_id": "demo-tenant",
        }
    }

    context = Context(user_name="Demo User 1")

    supervisor_agent = await create_supervisor_agent(
        context_schema=Context,
    )

    async for chunk in supervisor_agent.astream(
        input=messages,
        config=config,
        context=context,
        stream_mode=["updates"],
    ):
        print(chunk)


if __name__ == "__main__":
    import asyncio

    asyncio.run(check_agent())