import os

from pydantic import BaseModel


class Settings(BaseModel):
    STORE_DATABASE_URL: str = os.getenv("STORE_DATABASE_URL", "")


settings = Settings()