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
