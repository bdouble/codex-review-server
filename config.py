"""Configuration for the Codex Review MCP Server.

All settings read live from environment variables on each access.
A .env file in the server directory is re-loaded on each read,
so changes take effect on the next tool call without restarting.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

_ENV_FILE = Path(__file__).parent / ".env"


class classproperty:
    """Descriptor that works like @property but on the class itself."""
    def __init__(self, func):
        self.func = func
    def __get__(self, obj, objtype=None):
        return self.func(objtype)


def _reload_env():
    """Re-read .env file, overriding current env vars."""
    load_dotenv(_ENV_FILE, override=True)


class Config:
    """Server configuration. All properties read live from env vars."""

    @staticmethod
    def _get(key: str, default: str) -> str:
        _reload_env()
        return os.getenv(key, default)

    @classproperty
    def CODEX_HOME(cls) -> str:
        return os.path.expanduser(cls._get("CODEX_REVIEW_HOME", "~/.codex"))

    @classproperty
    def MODEL(cls) -> str:
        return cls._get("CODEX_REVIEW_MODEL", "gpt-5.3-codex")

    @classproperty
    def REASONING(cls) -> str:
        return cls._get("CODEX_REVIEW_REASONING", "xhigh")

    @classproperty
    def TIMEOUT(cls) -> int:
        return int(cls._get("CODEX_REVIEW_TIMEOUT", "1500"))

    @classproperty
    def FOCUS(cls) -> str:
        return cls._get("CODEX_REVIEW_FOCUS", "all")

    @classmethod
    def validate(cls) -> list[str]:
        """Validate current configuration. Returns list of warnings."""
        warnings = []

        if not (cls.MODEL.startswith("gpt-") or cls.MODEL.startswith("o")):
            warnings.append(
                f"CODEX_REVIEW_MODEL='{cls.MODEL}' does not look like an OpenAI model. "
                f"Expected prefix 'gpt-' or 'o'."
            )

        valid_reasoning = {"none", "low", "medium", "high", "xhigh"}
        if cls.REASONING not in valid_reasoning:
            warnings.append(
                f"CODEX_REVIEW_REASONING='{cls.REASONING}' is not valid. "
                f"Expected one of: {', '.join(sorted(valid_reasoning))}"
            )

        return warnings
