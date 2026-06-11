from uuid import uuid4

from langchain.messages import HumanMessage

from agents.shared.agent_contexts import Context

from agents.mail_agent.agent import create_mail_agent
from agents.shared.llms import get_llm


async def check_agent():

    user_input = (
        "Write a mail thanking the General for his service."
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

    context = Context(
        user_name="Demo User 1",
    )

    mail_agent = await create_mail_agent(
        llm=get_llm(),
        context_schema=Context,
    )

    async for chunk in mail_agent.astream(
        input=messages,
        config=config,
        context=context,
        stream_mode=["updates"],
    ):
        print(chunk)


if __name__ == "__main__":
    import asyncio

    asyncio.run(check_agent())