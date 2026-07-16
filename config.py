"""Configuration for the Codex Delegation MCP Server.

All settings read live from environment variables on each access.
A .env file in the server directory is re-loaded on each read,
so changes take effect on the next tool call without restarting.

Env vars use the CODEX_* prefix. The older CODEX_REVIEW_* names are still
honoured as a fallback so existing .env files keep working.
"""

import os
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

import models

_ENV_FILE = Path(__file__).parent / ".env"

DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_EFFORT = "xhigh"
DEFAULT_TIMEOUT = "4500"

# "none" was a valid reasoning level under the 5.3-era catalog and is listed as
# one in 1.0's .env.example. No model in the current catalog offers it, so an
# untouched legacy .env would fail validation on every single tool call and
# leave the server unable to run anything — the opposite of the compatibility
# the CODEX_REVIEW_* fallback exists to provide. Map it to the lowest level
# that does exist and warn.
LEGACY_EFFORTS = {"none": "low"}


def subprocess_env() -> dict:
    """Environment for child processes, with this server's own venv removed.

    The documented install runs this server from its own .venv, so children
    would inherit VIRTUAL_ENV and a PATH led by that venv's bin. A
    verify_command of `pytest` would then resolve against the *server's*
    environment rather than the target project's — reporting "No module named
    pytest" for a project that has pytest installed just fine.

    The worker is still launched via sys.executable explicitly, so stripping
    the venv from PATH here does not affect our own imports.
    """
    env = os.environ.copy()
    if sys.prefix != sys.base_prefix:
        venv_bin = os.path.normpath(os.path.join(sys.prefix, "bin"))
        env["PATH"] = os.pathsep.join(
            part
            for part in env.get("PATH", "").split(os.pathsep)
            if part and os.path.normpath(part) != venv_bin
        )
        env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    return env


class classproperty:
    """Descriptor that works like @property but on the class itself."""

    def __init__(self, func):
        self.func = func

    def __get__(self, obj, objtype=None):
        return self.func(objtype)


# What os.environ held for each key before .env last overrode it. load_dotenv
# only ever *adds* to os.environ, so without this a key deleted from .env keeps
# serving its old value out of the environment until the server restarts — and
# the whole point of re-reading the file is that an edit takes effect on the
# next tool call. Pinned to a deprecated model, you would comment the line out,
# see nothing change, and have no way to tell why.
_overridden: dict[str, str | None] = {}


def _reload_env():
    """Re-read .env, applying additions, edits, and removals alike."""
    values = {
        key: value
        for key, value in dotenv_values(_ENV_FILE).items()
        if value is not None
    }

    for key in list(_overridden):
        if key not in values:
            # Put back whatever the real environment had, so a key removed from
            # .env falls back to it rather than to a stale value or to nothing.
            original = _overridden.pop(key)
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original

    for key in values:
        _overridden.setdefault(key, os.environ.get(key))

    load_dotenv(_ENV_FILE, override=True)


class Config:
    """Server configuration. All properties read live from env vars."""

    @staticmethod
    def _get(key: str, default: str, legacy_key: str | None = None) -> str:
        _reload_env()
        value = os.getenv(key)
        if value is None and legacy_key:
            value = os.getenv(legacy_key)
        return default if value is None else value

    @classproperty
    def CODEX_HOME(cls) -> str:
        return os.path.expanduser(
            cls._get("CODEX_HOME_DIR", "~/.codex", legacy_key="CODEX_REVIEW_HOME")
        )

    @classproperty
    def MODEL(cls) -> str:
        return cls._get("CODEX_MODEL", DEFAULT_MODEL, legacy_key="CODEX_REVIEW_MODEL")

    @classproperty
    def EFFORT(cls) -> str:
        return LEGACY_EFFORTS.get(cls._raw_effort(), cls._raw_effort())

    @staticmethod
    def _raw_effort() -> str:
        return Config._get(
            "CODEX_EFFORT", DEFAULT_EFFORT, legacy_key="CODEX_REVIEW_REASONING"
        )

    @classproperty
    def TIMEOUT(cls) -> int:
        raw = cls._get(
            "CODEX_TIMEOUT", DEFAULT_TIMEOUT, legacy_key="CODEX_REVIEW_TIMEOUT"
        )
        try:
            return int(raw)
        except ValueError:
            return int(DEFAULT_TIMEOUT)

    @classproperty
    def FOCUS(cls) -> str:
        return cls._get("CODEX_FOCUS", "all", legacy_key="CODEX_REVIEW_FOCUS")

    @classproperty
    def STATE_DIR(cls) -> str:
        return os.path.expanduser(
            cls._get("CODEX_STATE_DIR", "~/.codex-review-server")
        )

    @classproperty
    def MAX_JOBS(cls) -> int:
        try:
            return int(cls._get("CODEX_MAX_JOBS", "50"))
        except ValueError:
            return 50

    @classmethod
    def validate(cls) -> list[str]:
        """Validate current configuration. Returns a list of warnings."""
        warnings = []

        raw_effort = cls._raw_effort()
        if raw_effort in LEGACY_EFFORTS:
            warnings.append(
                f"Reasoning effort '{raw_effort}' is no longer offered by any "
                f"model; using '{LEGACY_EFFORTS[raw_effort]}'. Update your .env."
            )

        error = models.validate(cls.MODEL, cls.EFFORT, cls.CODEX_HOME)
        if error:
            warnings.append(error)

        if cls.TIMEOUT <= 0:
            warnings.append(f"CODEX_TIMEOUT={cls.TIMEOUT} must be positive.")

        valid_focus = {"bugs", "security", "performance", "all"}
        if cls.FOCUS not in valid_focus:
            warnings.append(
                f"CODEX_FOCUS='{cls.FOCUS}' is not valid. "
                f"Expected one of: {', '.join(sorted(valid_focus))}"
            )

        return warnings
