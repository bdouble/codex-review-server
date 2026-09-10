"""Tests for the live model catalog and model/effort validation."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import models

SAMPLE = {
    "models": [
        {
            "slug": "gpt-5.6-terra",
            "display_name": "GPT-5.6-Terra",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [
                {"effort": "low"}, {"effort": "medium"}, {"effort": "high"},
                {"effort": "xhigh"}, {"effort": "max"}, {"effort": "ultra"},
            ],
            "visibility": "list",
        },
        {
            "slug": "gpt-5.6-luna",
            "display_name": "GPT-5.6-Luna",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [
                {"effort": "low"}, {"effort": "medium"}, {"effort": "high"},
                {"effort": "xhigh"}, {"effort": "max"},
            ],
            "visibility": "list",
        },
        {
            "slug": "codex-auto-review",
            "display_name": "Internal",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [{"effort": "medium"}],
            "visibility": "hide",
        },
    ]
}


@pytest.fixture(autouse=True)
def clear_cache():
    models._cache.clear()
    models._last_source = None
    yield
    models._cache.clear()
    models._last_source = None


def _stub_codex(monkeypatch, stdout, returncode=0):
    monkeypatch.setattr(models.shutil, "which", lambda _: "/usr/bin/codex")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    monkeypatch.setattr(models.subprocess, "run", fake_run)


class TestQueryCatalog:
    def test_parses_slugs_and_efforts(self, monkeypatch):
        _stub_codex(monkeypatch, json.dumps(SAMPLE))
        catalog = models.get_catalog()
        assert catalog["gpt-5.6-terra"]["efforts"][-1] == "ultra"
        assert catalog["gpt-5.6-terra"]["default_effort"] == "medium"

    def test_hidden_models_are_excluded(self, monkeypatch):
        # codex-auto-review is an internal model, not something to delegate to.
        _stub_codex(monkeypatch, json.dumps(SAMPLE))
        assert "codex-auto-review" not in models.get_catalog()

    def test_luna_has_no_ultra(self, monkeypatch):
        _stub_codex(monkeypatch, json.dumps(SAMPLE))
        assert "ultra" not in models.get_catalog()["gpt-5.6-luna"]["efforts"]

    def test_live_source_is_reported(self, monkeypatch):
        _stub_codex(monkeypatch, json.dumps(SAMPLE))
        models.get_catalog()
        assert models.catalog_source() == "live"


class TestFallback:
    def test_missing_binary_falls_back(self, monkeypatch):
        monkeypatch.setattr(models.shutil, "which", lambda _: None)
        assert models.get_catalog() == models.FALLBACK_CATALOG
        assert models.catalog_source() == "fallback"

    def test_bad_json_falls_back(self, monkeypatch):
        _stub_codex(monkeypatch, "not json at all")
        assert models.get_catalog() == models.FALLBACK_CATALOG

    def test_nonzero_exit_falls_back(self, monkeypatch):
        _stub_codex(monkeypatch, "", returncode=1)
        assert models.get_catalog() == models.FALLBACK_CATALOG

    def test_fallback_catalog_matches_verified_reality(self):
        # Verified against codex-cli 0.144.4 on 2026-07-15.
        assert models.FALLBACK_CATALOG["gpt-5.6-sol"]["default_effort"] == "low"
        assert "ultra" in models.FALLBACK_CATALOG["gpt-5.6-terra"]["efforts"]
        assert "ultra" not in models.FALLBACK_CATALOG["gpt-5.6-luna"]["efforts"]
        assert "max" not in models.FALLBACK_CATALOG["gpt-5.5"]["efforts"]


class TestCaching:
    def test_cache_is_keyed_by_codex_home(self, monkeypatch):
        # Multiple ChatGPT accounts are a documented workflow, and CODEX_HOME
        # is live-reloaded. A single shared cache slot served one account's
        # catalog to another for up to the TTL after a switch.
        seen = []
        monkeypatch.setattr(models.shutil, "which", lambda _: "/usr/bin/codex")

        def fake_run(cmd, **kwargs):
            home = kwargs.get("env", {}).get("CODEX_HOME")
            seen.append(home)
            slug = "model-a" if home == "/home/a" else "model-b"
            payload = {"models": [{
                "slug": slug,
                "default_reasoning_level": "low",
                "supported_reasoning_levels": [{"effort": "low"}],
                "visibility": "list",
            }]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

        monkeypatch.setattr(models.subprocess, "run", fake_run)
        assert "model-a" in models.get_catalog("/home/a")
        assert "model-b" in models.get_catalog("/home/b")
        assert seen == ["/home/a", "/home/b"]

    def test_second_call_does_not_reshell(self, monkeypatch):
        calls = []
        monkeypatch.setattr(models.shutil, "which", lambda _: "/usr/bin/codex")

        def fake_run(*args, **kwargs):
            calls.append(1)
            return subprocess.CompletedProcess(args, 0, json.dumps(SAMPLE), "")

        monkeypatch.setattr(models.subprocess, "run", fake_run)
        models.get_catalog()
        models.get_catalog()
        assert len(calls) == 1

    def test_force_refresh_requeries(self, monkeypatch):
        calls = []
        monkeypatch.setattr(models.shutil, "which", lambda _: "/usr/bin/codex")

        def fake_run(*args, **kwargs):
            calls.append(1)
            return subprocess.CompletedProcess(args, 0, json.dumps(SAMPLE), "")

        monkeypatch.setattr(models.subprocess, "run", fake_run)
        models.get_catalog()
        models.get_catalog(force_refresh=True)
        assert len(calls) == 2


class TestValidate:
    def test_valid_pair(self, monkeypatch):
        _stub_codex(monkeypatch, json.dumps(SAMPLE))
        assert models.validate("gpt-5.6-terra", "ultra") is None

    def test_effort_unsupported_by_model(self, monkeypatch):
        _stub_codex(monkeypatch, json.dumps(SAMPLE))
        error = models.validate("gpt-5.6-luna", "ultra")
        assert "not supported by gpt-5.6-luna" in error

    def test_deprecated_model_rejected_with_replacement(self, monkeypatch):
        _stub_codex(monkeypatch, json.dumps(SAMPLE))
        error = models.validate("gpt-5.3-codex", "xhigh")
        assert "deprecated" in error and "gpt-5.6-terra" in error

    def test_bare_alias_rejected(self, monkeypatch):
        # `gpt-5.6` resolves only under API-key auth; this server uses ChatGPT.
        _stub_codex(monkeypatch, json.dumps(SAMPLE))
        error = models.validate("gpt-5.6", "xhigh")
        assert "full slug" in error

    def test_unknown_model_is_allowed_through(self, monkeypatch):
        # Blocking unrecognized slugs would recreate the rot problem the live
        # catalog exists to solve. Codex is the final authority.
        _stub_codex(monkeypatch, json.dumps(SAMPLE))
        assert models.validate("gpt-5.9-future", "xhigh") is None

    def test_empty_effort_is_allowed(self, monkeypatch):
        _stub_codex(monkeypatch, json.dumps(SAMPLE))
        assert models.validate("gpt-5.6-terra", "") is None


class TestDescribe:
    def test_shape(self, monkeypatch):
        _stub_codex(monkeypatch, json.dumps(SAMPLE))
        described = models.describe()
        assert described["source"] == "live"
        assert "gpt-5.6-terra" in described["models"]
        assert "gpt-5.3-codex" in described["deprecated"]


# The catalog as codex-cli 0.154.0 reports it on an account approved for the
# access-gated Daybreak model. Verified live on 2026-09-10.
SAMPLE_0154 = {
    "models": [
        {
            "slug": "gpt-6-astra",
            "display_name": "GPT-6-Astra",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [
                {"effort": "low"}, {"effort": "medium"}, {"effort": "high"},
                {"effort": "xhigh"}, {"effort": "max"}, {"effort": "ultra"},
            ],
            "visibility": "list",
        },
        {
            "slug": "gpt-daybreak-blue-latest",
            "display_name": "Daybreak Blue",
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [
                {"effort": "low"}, {"effort": "medium"}, {"effort": "high"},
                {"effort": "xhigh"}, {"effort": "max"}, {"effort": "ultra"},
            ],
            "visibility": "list",
        },
        {
            "slug": "gpt-5.3-codex-spark",
            "display_name": "GPT-5.3-Codex-Spark",
            "default_reasoning_level": "high",
            "supported_reasoning_levels": [
                {"effort": "low"}, {"effort": "medium"}, {"effort": "high"},
                {"effort": "xhigh"},
            ],
            "visibility": "list",
        },
        {
            "slug": "gpt-reserve",
            "display_name": "GPT-Reserve",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [{"effort": "medium"}],
            "visibility": "hide",
        },
    ]
}


class TestCodex0154Catalog:
    def test_new_models_need_no_code_change(self, monkeypatch):
        # The whole point of reading the catalog live: gpt-6-astra and
        # Daybreak Blue shipped after this server was written and are usable
        # without touching models.py.
        _stub_codex(monkeypatch, json.dumps(SAMPLE_0154))
        catalog = models.get_catalog()
        assert "gpt-6-astra" in catalog
        assert "gpt-daybreak-blue-latest" in catalog

    def test_astra_supports_ultra(self, monkeypatch):
        _stub_codex(monkeypatch, json.dumps(SAMPLE_0154))
        assert models.validate("gpt-6-astra", "ultra") is None

    def test_daybreak_supports_ultra(self, monkeypatch):
        _stub_codex(monkeypatch, json.dumps(SAMPLE_0154))
        assert models.validate("gpt-daybreak-blue-latest", "ultra") is None

    def test_daybreak_display_name_is_carried_through(self, monkeypatch):
        _stub_codex(monkeypatch, json.dumps(SAMPLE_0154))
        described = models.describe()
        assert described["models"]["gpt-daybreak-blue-latest"]["display_name"] == (
            "Daybreak Blue"
        )

    def test_gpt_reserve_is_still_hidden(self, monkeypatch):
        _stub_codex(monkeypatch, json.dumps(SAMPLE_0154))
        assert "gpt-reserve" not in models.get_catalog()

    def test_spark_effort_ceiling_is_enforced(self, monkeypatch):
        # Spark tops out at xhigh even though the 5.6 family does not.
        _stub_codex(monkeypatch, json.dumps(SAMPLE_0154))
        error = models.validate("gpt-5.3-codex-spark", "ultra")
        assert "not supported by gpt-5.3-codex-spark" in error


class TestLiveCatalogOutranksStaticTables:
    """A slug the account can actually use must never be blocked by a constant.

    gpt-5.3-codex-spark was on DEPRECATED_MODELS as "not available on this
    account" while `codex debug models` listed it as available. Because the
    deny-list was consulted before the catalog, the model was unreachable
    through this server with a message that was simply false.
    """

    def test_a_live_listed_model_is_not_blocked_by_the_deny_list(self, monkeypatch):
        _stub_codex(monkeypatch, json.dumps(SAMPLE_0154))
        monkeypatch.setitem(
            models.DEPRECATED_MODELS, "gpt-5.3-codex-spark", "stale claim"
        )
        assert models.validate("gpt-5.3-codex-spark", "xhigh") is None

    def test_a_live_listed_slug_is_not_blocked_by_the_alias_hint(self, monkeypatch):
        # If OpenAI ever makes the bare alias resolve, the catalog says so
        # first and the hint must get out of the way.
        payload = {"models": [{
            "slug": "gpt-5.6",
            "display_name": "GPT-5.6",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [{"effort": "xhigh"}],
            "visibility": "list",
        }]}
        _stub_codex(monkeypatch, json.dumps(payload))
        assert models.validate("gpt-5.6", "xhigh") is None

    def test_a_slug_absent_from_the_catalog_is_still_rejected(self, monkeypatch):
        # The deny-list still earns its keep for genuinely dead slugs.
        _stub_codex(monkeypatch, json.dumps(SAMPLE_0154))
        error = models.validate("gpt-5.3-codex", "xhigh")
        assert "deprecated" in error

    def test_spark_is_no_longer_deny_listed(self):
        assert "gpt-5.3-codex-spark" not in models.DEPRECATED_MODELS

    def test_retired_54_family_is_deny_listed_with_a_replacement(self):
        # Dropped from the live catalog in the 0.154.0 era. Naming a successor
        # beats letting codex answer with a bare 400 twenty minutes in.
        assert "gpt-5.6-luna" in models.DEPRECATED_MODELS["gpt-5.4-mini"]
        assert "gpt-5.5" in models.DEPRECATED_MODELS["gpt-5.4"]


class TestFallbackCatalogMatches0154:
    def test_fallback_carries_the_current_generation(self):
        assert "gpt-6-astra" in models.FALLBACK_CATALOG
        assert "gpt-daybreak-blue-latest" in models.FALLBACK_CATALOG

    def test_fallback_drops_the_retired_54_family(self):
        assert "gpt-5.4" not in models.FALLBACK_CATALOG
        assert "gpt-5.4-mini" not in models.FALLBACK_CATALOG

    def test_fallback_and_deny_list_never_contradict_each_other(self):
        # A slug in both tables is a bug: the fallback offers it while the
        # deny-list refuses it, and which one wins depends on whether codex
        # happened to be reachable.
        overlap = set(models.FALLBACK_CATALOG) & set(models.DEPRECATED_MODELS)
        assert overlap == set(), f"contradictory entries: {sorted(overlap)}"

    def test_astra_and_daybreak_validate_offline(self, monkeypatch):
        # With codex unreachable the fallback is all we have; the models the
        # user actually runs must still pass validation.
        monkeypatch.setattr(models.shutil, "which", lambda _: None)
        assert models.validate("gpt-6-astra", "ultra") is None
        assert models.validate("gpt-daybreak-blue-latest", "ultra") is None


class TestModelDescriptions:
    """Codex's own one-liner about each model, carried through to callers.

    Routing advice that lives in this repo goes stale the moment OpenAI ships
    a model. The catalog already says what each one is for; passing it along
    is how a caller learns that Daybreak Blue is the defensive-security model
    without anyone editing a table here.
    """

    def test_description_is_parsed_from_the_live_catalog(self, monkeypatch):
        payload = {"models": [{
            "slug": "gpt-daybreak-blue-latest",
            "display_name": "Daybreak Blue",
            "description": "Latest frontier agentic coding model for broad "
                           "defensive cybersecurity work.",
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [{"effort": "low"}],
            "visibility": "list",
        }]}
        _stub_codex(monkeypatch, json.dumps(payload))
        described = models.describe()
        assert "defensive cybersecurity" in (
            described["models"]["gpt-daybreak-blue-latest"]["description"]
        )

    def test_a_model_without_a_description_gets_an_empty_string(self, monkeypatch):
        _stub_codex(monkeypatch, json.dumps(SAMPLE))
        described = models.describe()
        assert described["models"]["gpt-5.6-terra"]["description"] == ""

    def test_every_fallback_entry_carries_one(self):
        missing = [
            slug for slug, entry in models.FALLBACK_CATALOG.items()
            if not entry.get("description")
        ]
        assert missing == [], f"no description for: {missing}"
