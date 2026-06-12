from dataclasses import dataclass, field
from typing import Any

@dataclass(slots=True)
class AgentSpec:
    name: str
    system_prompt: str | None = None
    tools: list[Any] = field(default_factory=list)
    response_format: Any = None
    context_schema: type | None = None
