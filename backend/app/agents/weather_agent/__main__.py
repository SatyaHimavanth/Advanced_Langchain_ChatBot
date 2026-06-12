from uuid import uuid4

from langchain.messages import HumanMessage

from app.agents.shared.agent_contexts import Context
from app.agents.weather_agent.agent import create_weather_agent
from app.agents.shared.llms import get_llm


async def check_agent():
    user_input = "What's the weather like in Tokyo today?"

    messages = {"messages": [HumanMessage(content=user_input)]}

    config = {
        "configurable": {
            "thread_id": str(uuid4()),
            "user_id": "demo-user",
            "tenant_id": "demo-tenant",
        }
    }

    context = Context(user_name="Demo User 1")

    weather_agent = await create_weather_agent(
        llm=get_llm(),
        context_schema=Context,
    )

    async for chunk in weather_agent.astream(
        input=messages,
        config=config,
        context=context,
        stream_mode=["updates"],
    ):
        print(chunk)


if __name__ == "__main__":
    import asyncio

    asyncio.run(check_agent())
