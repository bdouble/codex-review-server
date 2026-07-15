"""Tests for git-grounded verification."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import verify


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t.co"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "init"], tmp_path)
    return tmp_path


class TestParsePorcelain:
    def test_unstaged_modification_keeps_full_path(self):
        # Regression: `git status --porcelain` puts status in the first two
        # columns, so an unstaged edit starts with a space. Stripping the raw
        # output shifted the parse and turned "calc.py" into "alc.py".
        entries = verify._parse_porcelain(" M calc.py\n")
        assert entries == {"calc.py": " M"}

    def test_untracked_file(self):
        assert verify._parse_porcelain("?? new.py\n") == {"new.py": "??"}

    def test_staged_and_modified(self):
        assert verify._parse_porcelain("MM calc.py\n") == {"calc.py": "MM"}

    def test_rename_uses_new_path(self):
        entries = verify._parse_porcelain("R  old.py -> new.py\n")
        assert entries == {"new.py": "R "}

    def test_multiple_entries(self):
        entries = verify._parse_porcelain(" M calc.py\n?? test_calc.py\nA  x.py\n")
        assert entries == {"calc.py": " M", "test_calc.py": "??", "x.py": "A "}

    def test_ignores_blank_and_short_lines(self):
        assert verify._parse_porcelain("\n\nx\n") == {}


class TestGitHelper:
    def test_git_returns_unstripped_stdout(self, repo):
        (repo / "calc.py").write_text("changed\n")
        ok, out = verify._git(["status", "--porcelain"], str(repo))
        assert ok
        # The leading status column must survive.
        assert out.startswith(" M")


class TestSnapshot:
    def test_detects_non_repo(self, tmp_path):
        assert verify.snapshot(str(tmp_path))["is_repo"] is False

    def test_clean_repo(self, repo):
        snap = verify.snapshot(str(repo))
        assert snap["is_repo"] is True
        assert snap["entries"] == {}
        assert len(snap["head"]) == 40


class TestVerify:
    def test_read_only_task_with_no_changes_passes(self, repo):
        before = verify.snapshot(str(repo))
        report = verify.verify(str(repo), before, write=False)
        assert report["verified"] is True
        assert report["git"]["files_changed"] == []

    def test_read_only_violation_fails(self, repo):
        before = verify.snapshot(str(repo))
        (repo / "calc.py").write_text("mutated\n")
        report = verify.verify(str(repo), before, write=False)
        assert report["verified"] is False
        check = next(c for c in report["checks"] if c["name"] == "read_only_respected")
        assert check["status"] == "fail"

    def test_write_task_reports_changed_files_correctly(self, repo):
        before = verify.snapshot(str(repo))
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n# edit\n")
        (repo / "new.py").write_text("x = 1\n")
        report = verify.verify(str(repo), before, write=True)
        # Regression guard for the truncated-path bug.
        assert report["git"]["files_changed"] == ["calc.py", "new.py"]
        assert report["verified"] is True

    def test_write_task_with_no_changes_warns_but_does_not_fail(self, repo):
        before = verify.snapshot(str(repo))
        report = verify.verify(str(repo), before, write=True)
        check = next(c for c in report["checks"] if c["name"] == "files_changed")
        assert check["status"] == "warn"
        # A warning is informative, not a failure: some write tasks correctly
        # conclude no change is needed.
        assert report["verified"] is True
        assert report["warnings"]

    def test_detects_further_edits_to_an_already_dirty_file(self, repo):
        # Regression: delegating against a dirty tree is normal. An already
        # modified file stays " M" when edited again, so a porcelain-only
        # comparison saw no change — missing the edit AND falsely warning that
        # Codex claimed work it hadn't done.
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n# mine\n")
        before = verify.snapshot(str(repo))
        assert before["entries"] == {"calc.py": " M"}

        (repo / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n# mine\n# codex edit\n"
        )
        report = verify.verify(str(repo), before, write=True)
        assert report["git"]["files_changed"] == ["calc.py"]
        check = next(c for c in report["checks"] if c["name"] == "files_changed")
        assert check["status"] == "pass"

    def test_detects_same_line_count_edit_to_a_dirty_file(self, repo):
        # Regression: neither porcelain status (" M" either way) nor numstat
        # ("1,0" either way) moves when an edit replaces a line. Only content
        # hashing catches it.
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n# mine\n")
        before = verify.snapshot(str(repo))
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n# CODEX\n")
        assert verify._numstat(str(repo)) == before["numstat"]  # numstat blind
        report = verify.verify(str(repo), before, write=True)
        assert report["git"]["files_changed"] == ["calc.py"]

    def test_detects_edit_to_an_already_untracked_file(self, repo):
        # Untracked files never appear in numstat at all.
        (repo / "scratch.py").write_text("x = 1\n")
        before = verify.snapshot(str(repo))
        assert before["entries"] == {"scratch.py": "??"}
        (repo / "scratch.py").write_text("x = 2\n")
        report = verify.verify(str(repo), before, write=True)
        assert report["git"]["files_changed"] == ["scratch.py"]

    def test_git_failure_is_not_reported_as_verified(self, repo, monkeypatch):
        # "Could not inspect" must never become "nothing changed".
        before = verify.snapshot(str(repo))
        real_git = verify._git

        def flaky(args, cwd, timeout=60):
            if args[:2] == ["status", "--porcelain"]:
                return False, ""
            return real_git(args, cwd, timeout)

        monkeypatch.setattr(verify, "_git", flaky)
        report = verify.verify(str(repo), before, write=False)
        assert report["verified"] is False
        check = next(c for c in report["checks"] if c["name"] == "git_tracking")
        assert check["status"] == "fail"

    def test_read_only_violation_detected_on_dirty_tree(self, repo):
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n# mine\n")
        before = verify.snapshot(str(repo))
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n# mine\n# x\n")
        report = verify.verify(str(repo), before, write=False)
        assert report["verified"] is False

    def test_untouched_dirty_file_is_not_reported_as_changed(self, repo):
        # The pre-existing edit is the user's, not the task's — don't claim it.
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n# mine\n")
        before = verify.snapshot(str(repo))
        report = verify.verify(str(repo), before, write=True)
        assert report["git"]["files_changed"] == []

    def test_detects_committed_work(self, repo):
        before = verify.snapshot(str(repo))
        (repo / "committed.py").write_text("y = 2\n")
        _git(["add", "-A"], repo)
        _git(["commit", "-qm", "codex work"], repo)
        report = verify.verify(str(repo), before, write=True)
        assert report["git"]["committed"] is True
        assert "committed.py" in report["git"]["files_changed"]

    def test_non_repo_skips_git_checks(self, tmp_path):
        before = verify.snapshot(str(tmp_path))
        report = verify.verify(str(tmp_path), before, write=True)
        assert report["git"]["is_repo"] is False
        assert report["checks"][0]["status"] == "skip"
        assert report["verified"] is True


class TestVerifyCommand:
    def test_passing_command(self, repo):
        before = verify.snapshot(str(repo))
        report = verify.verify(
            str(repo), before, write=False, verify_command="exit 0"
        )
        check = next(c for c in report["checks"] if c["name"] == "verify_command")
        assert check["status"] == "pass"
        assert report["verified"] is True

    def test_failing_command_fails_the_job(self, repo):
        before = verify.snapshot(str(repo))
        report = verify.verify(
            str(repo), before, write=False, verify_command="echo boom >&2; exit 1"
        )
        check = next(c for c in report["checks"] if c["name"] == "verify_command")
        assert check["status"] == "fail"
        assert "boom" in check["output_tail"]
        assert report["verified"] is False

    def test_command_runs_in_project_dir(self, repo):
        result = verify.run_verify_command("pwd", str(repo))
        assert result["passed"] is True
        assert str(repo) in result["output_tail"]

    def test_timeout_is_reported_not_raised(self, repo):
        result = verify.run_verify_command("sleep 5", str(repo), timeout=1)
        assert result["passed"] is False
        assert "timed out" in result["output_tail"]

    def test_server_venv_not_leaked_to_command(self, repo):
        # The server runs from its own venv; a verify_command must see the
        # project's environment, not ours.
        result = verify.run_verify_command("echo $VIRTUAL_ENV", str(repo))
        assert result["output_tail"].strip() == ""
