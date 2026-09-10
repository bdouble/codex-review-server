"""Codex model catalog.

The catalog is read live from `codex debug models` rather than hardcoded.
This matters: the previous hardcoded default (`gpt-5.3-codex`) was deprecated
server-side by OpenAI and every call using it started failing with a 400.
Reading the catalog live means new models work without a code change, and
deprecated ones are caught before we spend 20 minutes on a doomed run.

Effort validity is per-model — gpt-5.6-luna supports `max` but not `ultra`,
and gpt-5.5 / gpt-5.3-codex-spark support neither.
"""

import json
import os
import shutil
import subprocess
import time

# Verified against codex-cli 0.154.0 on 2026-09-10 via `codex debug models`.
# Only used when the live query fails (codex missing, offline, format change).
#
# Entries here are a last-known-good snapshot, never an allow-list: the live
# catalog is always preferred, and `validate` lets unknown slugs through.
FALLBACK_CATALOG = {
    "gpt-6-astra": {
        "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
        "default_effort": "medium",
        "display_name": "GPT-6-Astra",
        "description": "Our most capable model for complex, demanding work.",
    },
    "gpt-5.6-sol": {
        "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
        "default_effort": "low",
        "display_name": "GPT-5.6-Sol",
        "description": "Frontier model for ambiguous, high-value work.",
    },
    "gpt-5.6-terra": {
        "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
        "default_effort": "medium",
        "display_name": "GPT-5.6-Terra",
        "description": "Pragmatic all-rounder for everyday engineering.",
    },
    "gpt-5.6-luna": {
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default_effort": "medium",
        "display_name": "GPT-5.6-Luna",
        "description": "Fast model for extraction, classification, and structured summaries.",
    },
    # Access-gated: present only on accounts approved for it, so it is absent
    # from most live catalogs. Listing it here is harmless — the fallback is
    # only consulted when the live query fails, and unknown-to-the-account
    # slugs are rejected by codex itself, not by us.
    "gpt-daybreak-blue-latest": {
        "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
        "default_effort": "low",
        "display_name": "Daybreak Blue",
        "description": "Latest frontier agentic coding model for broad defensive cybersecurity work.",
    },
    "gpt-5.5": {
        "efforts": ["low", "medium", "high", "xhigh"],
        "default_effort": "medium",
        "display_name": "GPT-5.5",
        "description": "Previous-generation all-rounder.",
    },
    "gpt-5.3-codex-spark": {
        "efforts": ["low", "medium", "high", "xhigh"],
        "default_effort": "high",
        "display_name": "GPT-5.3-Codex-Spark",
        "description": "Ultra-fast coding model.",
    },
}

# Known-dead slugs, so we can fail with a useful message instead of a raw 400.
#
# This is a hint of last resort, not an authority: `validate` consults the live
# catalog first and lets anything listed there through. A slug that OpenAI
# brings back — gpt-5.3-codex-spark was on this list while being live-listed
# and account-available — must not stay blocked by a stale constant.
DEPRECATED_MODELS = {
    "gpt-5.3-codex": "Deprecated by OpenAI for ChatGPT-account auth. Use gpt-5.6-terra.",
    "gpt-5.2": "Deprecated by OpenAI for ChatGPT-account auth. Use gpt-5.6-terra.",
    "gpt-5.4": "Retired from the ChatGPT-account catalog. Use gpt-5.5 or gpt-5.6-terra.",
    "gpt-5.4-mini": "Retired from the ChatGPT-account catalog. Use gpt-5.6-luna.",
}

# The bare alias resolves only under API-key auth; this server uses ChatGPT auth.
ALIAS_HINTS = {
    "gpt-5.6": "gpt-5.6-terra",
    "gpt-5.6-codex": "gpt-5.6-terra",
}

_CACHE_TTL_SECONDS = 300
# Keyed by codex_home: different ChatGPT accounts have different catalogs, and
# CODEX_HOME_DIR is live-reloaded, so a single shared slot would serve one
# account's models to another for up to the TTL after a switch.
_cache: dict = {}
_last_source: str | None = None


def _query_catalog(codex_home: str | None = None) -> dict | None:
    """Query the live catalog from `codex debug models`. None if unavailable."""
    codex_bin = shutil.which("codex")
    if codex_bin is None:
        return None

    env = os.environ.copy()
    if codex_home:
        env["CODEX_HOME"] = codex_home

    try:
        result = subprocess.run(
            [codex_bin, "debug", "models"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    catalog = {}
    for entry in payload.get("models", []):
        slug = entry.get("slug")
        if not slug:
            continue
        # `hide` entries are internal (e.g. codex-auto-review) — not for delegation.
        if entry.get("visibility") == "hide":
            continue
        efforts = [
            level["effort"]
            for level in entry.get("supported_reasoning_levels", [])
            if isinstance(level, dict) and level.get("effort")
        ]
        if not efforts:
            continue
        catalog[slug] = {
            "efforts": efforts,
            "default_effort": entry.get("default_reasoning_level") or efforts[0],
            "display_name": entry.get("display_name") or slug,
            # Codex's own one-line summary of what the model is for. Carried
            # through so routing advice comes from the catalog rather than from
            # a table in this repo that goes stale the moment OpenAI ships
            # something — it is how a caller learns that Daybreak Blue is the
            # defensive-security model and Spark the ultra-fast one.
            "description": entry.get("description") or "",
        }

    return catalog or None


def get_catalog(codex_home: str | None = None, force_refresh: bool = False) -> dict:
    """Return the model catalog for this codex_home, cached for 5 minutes.

    Falls back to FALLBACK_CATALOG if the live query fails.
    """
    global _last_source

    key = codex_home or ""
    now = time.monotonic()
    entry = _cache.get(key)
    if (
        not force_refresh
        and entry is not None
        and now - entry["fetched_at"] < _CACHE_TTL_SECONDS
    ):
        _last_source = entry["source"]
        return entry["catalog"]

    live = _query_catalog(codex_home)
    catalog = live or FALLBACK_CATALOG
    source = "live" if live else "fallback"
    _cache[key] = {"fetched_at": now, "catalog": catalog, "source": source}
    _last_source = source
    return catalog


def catalog_source() -> str:
    """Where the most recently returned catalog came from."""
    return _last_source or "unknown"


def validate(model: str, effort: str, codex_home: str | None = None) -> str | None:
    """Validate a model/effort pair. Returns an error message, or None if valid.

    Unknown models are allowed through with no error — the catalog may be newer
    than this code, and blocking an unrecognized slug would recreate the very
    rot problem the live catalog exists to solve. Known-dead slugs are rejected.

    The live catalog outranks both static tables. A slug the account can
    actually use is never blocked by DEPRECATED_MODELS or ALIAS_HINTS, because
    those constants rot in exactly the direction that hurts: gpt-5.3-codex-spark
    sat on the deny-list, unreachable, while `codex debug models` listed it as
    available. Only consult them for slugs the live catalog does not know.
    """
    catalog = get_catalog(codex_home)
    entry = catalog.get(model)

    if entry is None:
        if model in DEPRECATED_MODELS:
            return f"Model '{model}' is deprecated: {DEPRECATED_MODELS[model]}"

        if model in ALIAS_HINTS:
            return (
                f"'{model}' is an alias that does not resolve under "
                f"ChatGPT-account auth. Use the full slug, e.g. "
                f"'{ALIAS_HINTS[model]}'."
            )

        # Unknown but not known-dead: allow. Codex itself is the final authority.
        return None

    if effort and effort not in entry["efforts"]:
        return (
            f"Effort '{effort}' is not supported by {model}. "
            f"Supported: {', '.join(entry['efforts'])}."
        )

    return None


def describe(codex_home: str | None = None) -> dict:
    """Catalog snapshot for the codex_models tool."""
    catalog = get_catalog(codex_home)
    return {
        "source": catalog_source(),
        "models": {
            slug: {
                "display_name": entry["display_name"],
                "description": entry.get("description", ""),
                "efforts": entry["efforts"],
                "default_effort": entry["default_effort"],
            }
            for slug, entry in catalog.items()
        },
        "deprecated": DEPRECATED_MODELS,
    }
