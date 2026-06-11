from uuid import uuid4

from langchain.messages import HumanMessage

from agents.shared.agent_contexts import Context
from agents.coding_agent.agent import create_coding_agent
from agents.shared.llms import get_llm


async def check_agent():
    user_input = (
        "Hi, can you generate a file with a python function to print all prime "
        "numbers in a given range and save it as primes.py? "
        "Then return the complete file path and check whether the file exists."
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

    coding_agent = await create_coding_agent(
        llm=get_llm(),
        context_schema=Context,
    )

    async for chunk in coding_agent.astream(
        input=messages,
        config=config,
        context=context,
        stream_mode=["updates"],
    ):
        print(chunk)


if __name__ == "__main__":
    import asyncio

    asyncio.run(check_agent())