from uuid import uuid4

from langchain.messages import HumanMessage

from app.agents.shared.agent_contexts import Context
from app.agents.main_agent.agent import create_main_agent
from app.agents.shared.llms import get_llm
from app.agents.shared.memory import open_agent_persistence
from app.settings import settings


async def check_agent():
    user_input = (
        "Hi, can you generate a file with a python function to print all prime "
        "numbers in a given range and save it as primes.py? "
        "Then run it to verify it works and report any errors."
    )

    messages = {"messages": [HumanMessage(content=user_input)]}

    config = {
        "configurable": {
            "thread_id": str(uuid4()),
            "user_id": "demo-user",
            "tenant_id": "demo-tenant",
        }
    }

    context = Context(user_name="Demo User 1")

    async with open_agent_persistence(settings.STORE_DATABASE_URL) as (store, checkpointer):
        main_agent = await create_main_agent(
            llm=get_llm(),
            context_schema=Context,
            store=store,
            checkpointer=checkpointer,
        )

        async for chunk in main_agent.astream(
            input=messages,
            config=config,
            context=context,
            stream_mode=["updates"],
        ):
            print(chunk)


if __name__ == "__main__":
    import asyncio

    asyncio.run(check_agent())
