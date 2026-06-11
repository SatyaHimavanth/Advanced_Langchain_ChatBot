from dataclasses import dataclass


@dataclass
class Context:
    user_name: str = "Default User"
    user_email: str = "default_user@example.com"
    user_id: str = "1234"