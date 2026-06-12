import random
from typing import Literal

from langchain_core.tools import tool

from app.logger import get_logger

logger = get_logger(__name__)

Units = Literal["metric", "imperial"]

_CONDITIONS = [
    "clear sky",
    "partly cloudy",
    "overcast",
    "light rain",
    "thunderstorms",
    "snow",
    "foggy",
]


@tool
def get_weather(city: str, units: Units = "metric") -> dict:
    """
    Get the current weather and a short forecast for a city.

    This is a self-contained mock (no external API key required) so the agent
    can demonstrate tool use deterministically. Replace the body with a real
    weather provider call when an API key is available.

    Args:
        city: The city name to look up.
        units: "metric" (Celsius) or "imperial" (Fahrenheit).

    Returns:
        A dict with the current condition, temperature, humidity and a 3-day
        forecast.
    """
    rng = random.Random(city.lower())
    base_c = rng.randint(-5, 35)

    def to_unit(c: int) -> int:
        return c if units == "metric" else round(c * 9 / 5 + 32)

    symbol = "°C" if units == "metric" else "°F"

    forecast = [
        {
            "day": day,
            "condition": rng.choice(_CONDITIONS),
            "high": f"{to_unit(base_c + rng.randint(0, 5))}{symbol}",
            "low": f"{to_unit(base_c - rng.randint(0, 5))}{symbol}",
        }
        for day in ("today", "tomorrow", "day after")
    ]

    return {
        "city": city,
        "units": units,
        "current": {
            "condition": rng.choice(_CONDITIONS),
            "temperature": f"{to_unit(base_c)}{symbol}",
            "humidity": f"{rng.randint(30, 95)}%",
        },
        "forecast": forecast,
    }


logger.info("Successfully created `get_weather` tool.")

tools = [get_weather]
