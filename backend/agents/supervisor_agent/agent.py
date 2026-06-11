from langgraph_supervisor import create_supervisor

from agents.coding_agent.agent import create_coding_agent
from agents.mail_agent.agent import create_mail_agent
from agents.supervisor_agent.prompts import SYSTEM_PROMPT
from agents.shared.llms import get_llm
from agents.shared.memory import checkpointer
from logger import get_logger

logger = get_logger(__name__)


async def create_supervisor_agent(
    *,
    context_schema=None,
    database_url: str | None = None,
    store=None,
):
    try:
        logger.info("Creating child agents.")

        coding_agent = await create_coding_agent(
            llm=get_llm(),
            context_schema=context_schema,
            database_url=database_url,
            store=store,
        )

        mail_agent = await create_mail_agent(
            llm=get_llm(),
            context_schema=context_schema,
            database_url=database_url,
            store=store,
        )

        logger.info("Creating supervisor workflow.")
        workflow = create_supervisor(
            [coding_agent, mail_agent],
            model=get_llm(),
            prompt=SYSTEM_PROMPT,
            checkpointer=checkpointer,
        )

        logger.info("Compiling supervisor workflow.")
        return workflow.compile()

    except Exception:
        logger.exception("Error while creating supervisor agent")
        raise