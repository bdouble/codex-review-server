"""Configuration for the Codex Review MCP Server.

All settings read live from environment variables on each access.
A .env file in the server directory is re-loaded on each read,
so changes take effect on the next tool call without restarting.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

_ENV_FILE = Path(__file__).parent / ".env"

# Severity ordering (static — no reason to make this configurable)
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Allowed file extensions for review (static)
ALLOWED_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs", ".java",
    ".rb", ".php", ".swift", ".kt", ".cs", ".c", ".cpp", ".h",
    ".html", ".css", ".scss", ".less", ".json", ".yaml", ".yml",
    ".toml", ".xml", ".sql", ".sh", ".bash", ".zsh", ".md",
    ".txt", ".env.example", ".gitignore", ".dockerignore",
    ".tf", ".tfvars", ".prisma", ".graphql", ".proto",
}

# Blocked paths (static)
BLOCKED_PATHS = {
    ".ssh", ".gnupg", ".aws", ".env", ".netrc", "credentials",
    "secrets", ".git/config", ".claude/", ".codex/", ".config",
    ".kube", ".docker/config",
}


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
    def MIN_SEVERITY(cls) -> str:
        return cls._get("CODEX_REVIEW_MIN_SEVERITY", "medium")

    @classproperty
    def FOCUS(cls) -> str:
        return cls._get("CODEX_REVIEW_FOCUS", "all")

    @classproperty
    def AUTO_FIX(cls) -> bool:
        return cls._get("CODEX_REVIEW_AUTO_FIX", "false").lower() == "true"

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

        if cls.MIN_SEVERITY not in SEVERITY_ORDER:
            warnings.append(
                f"CODEX_REVIEW_MIN_SEVERITY='{cls.MIN_SEVERITY}' is not valid. "
                f"Expected one of: {', '.join(SEVERITY_ORDER.keys())}"
            )

        return warnings
