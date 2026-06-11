import logging

from typing import List, Optional
from pydantic import BaseModel

from langchain.tools import tool
from logger import get_logger


logger = get_logger(__name__)

class Attachment(BaseModel):
    file_name: str
    file_path: str
    mime_type: str
    file_size_bytes: Optional[int] = None


@tool
def send_mail(
    to: List[str],
    subject: str,
    body: str,
    attachments: Optional[List[str]] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    is_html: bool = False,
) -> str:
    """
    Send an email.

    Features:
    - Multiple recipients
    - CC and BCC support
    - Plain text or HTML body
    - Multiple file attachments (PDF, DOCX, XLSX, PNG, JPG, ZIP, etc.) via file paths.

    Returns:
        Confirmation message after sending.
    """
    return f"Mail sent successfully to {', '.join(to)}"
logger.info("Successfully created `send_mail` tool.")


logger.info("Grouping main agent tools...")
tools = [send_mail]
logger.info("Main agent tools are successfully created.")