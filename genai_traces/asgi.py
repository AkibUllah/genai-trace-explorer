"""ASGI entry point: ``uvicorn genai_traces.asgi:app``."""

from __future__ import annotations

import logging

from .api import create_app
from .config import Settings

settings = Settings()
logging.basicConfig(
    level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
app = create_app(settings)
