import logging
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()


LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

log_file = LOG_DIR / f"{datetime.now():%d-%m-%Y_%H-%M-%S-%f}.log"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler()  # Also print to console
    ]
)


def get_logger(name):
    return logging.getLogger(name)