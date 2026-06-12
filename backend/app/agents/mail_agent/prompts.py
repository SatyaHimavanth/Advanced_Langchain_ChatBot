import logging

from app.logger import get_logger


logger = get_logger(__name__)

logger.info("Initializing agent system prompt.")
SYSTEM_PROMPT = (
    "You are a mailing assistant with various tools to assist you in sending mail. "
    "Draft and send mails as required by user. "
    "For any other queries thare are not related to sending mail respond politely with your capabilities instead."
)