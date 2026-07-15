"""Verification of delegated work.

Codex saying "done" is a claim, not a result. Both of the reference
implementations for this pattern either trust the model's self-report or
mandate verification in prose and never enforce it. This module checks the
claim against the repository: what git says actually changed, and whether the
project's own test command still passes.

Every check is grounded in observable state, never in Codex's narration.
"""

import hashlib
import subprocess
from pathlib import Path

from config import subprocess_env


def _git(args: list[str], cwd: str, timeout: int = 60) -> tuple[bool, str]:
    """Run a git command. Returns (ok, raw stdout). Never uses a shell.

    stdout is returned unstripped on purpose. `git status --porcelain` encodes
    status in the first two columns, so an unstaged edit begins with a space
    (" M calc.py"). Stripping here would eat that column and shift every path
    parse by one character.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False, ""
    return result.returncode == 0, result.stdout


def _parse_porcelain(text: str) -> dict[str, str]:
    """Parse `git status --porcelain` into {path: status}."""
    entries = {}
    for line in text.splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2], line[3:]
        # Renames/copies render as "old -> new"; the new path is what exists now.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        entries[path.strip().strip('"')] = status
    return entries


def _numstat(project_dir: str) -> dict[str, str]:
    """Per-file {path: "added,deleted"} for tracked changes against HEAD."""
    ok, out = _git(["diff", "HEAD", "--numstat"], project_dir)
    if not ok:
        return {}
    stats = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip():
            stats[parts[2].strip()] = f"{parts[0]},{parts[1]}"
    return stats


def _hash_files(project_dir: str, paths) -> dict[str, str]:
    """Content hashes for the given paths.

    Status and line counts both go blind on a file that was *already* dirty
    before the task: editing it again leaves porcelain at " M", and a
    same-line-count edit leaves numstat identical too. Either way the change
    is invisible — a missed detection plus a false "Codex may be claiming work
    it did not do" warning. Content hashes are the only signal that always
    moves when the bytes move.

    Only baseline-dirty and untracked paths are hashed, never the whole repo,
    so this stays cheap: a clean file cannot change without git noticing.
    """
    hashes = {}
    root = Path(project_dir)
    for path in paths:
        target = root / path
        try:
            if target.is_file():
                hashes[path] = hashlib.sha256(target.read_bytes()).hexdigest()
            else:
                # Directory entry (porcelain lists untracked dirs) or deleted.
                hashes[path] = "<absent>"
        except OSError:
            hashes[path] = "<unreadable>"
    return hashes


def snapshot(project_dir: str) -> dict:
    """Capture repository state before a task runs."""
    is_repo, _ = _git(["rev-parse", "--is-inside-work-tree"], project_dir)
    if not is_repo:
        return {"is_repo": False}

    _, head = _git(["rev-parse", "HEAD"], project_dir)
    ok, porcelain = _git(["status", "--porcelain"], project_dir)
    entries = _parse_porcelain(porcelain) if ok else {}
    return {
        "is_repo": True,
        "head": head.strip(),
        "entries": entries,
        "numstat": _numstat(project_dir),
        "hashes": _hash_files(project_dir, entries.keys()),
        "git_ok": ok,
    }


def _changed_files(project_dir: str, before: dict, after_entries: dict,
                   after_numstat: dict, head_before: str,
                   head_after: str) -> list[str]:
    """Files the task actually touched, per git.

    Four signals, because no single one is sufficient:
      - porcelain delta: new, deleted, and newly-modified files
      - numstat delta: further edits to already-tracked-dirty files
      - content hashes: edits that move no line counts, and edits to files
        that were already untracked (which never appear in numstat at all)
      - HEAD diff: work the task committed itself, which would otherwise leave
        a clean tree and look like nothing happened
    """
    before_entries = before.get("entries", {})
    changed = {
        path
        for path in set(before_entries) | set(after_entries)
        if before_entries.get(path) != after_entries.get(path)
    }

    before_numstat = before.get("numstat", {})
    changed.update(
        path
        for path in set(before_numstat) | set(after_numstat)
        if before_numstat.get(path) != after_numstat.get(path)
    )

    before_hashes = before.get("hashes", {})
    if before_hashes:
        after_hashes = _hash_files(project_dir, before_hashes.keys())
        changed.update(
            path
            for path in before_hashes
            if before_hashes[path] != after_hashes.get(path)
        )

    if head_before and head_after and head_before != head_after:
        ok, names = _git(
            ["diff", "--name-only", f"{head_before}..{head_after}"], project_dir
        )
        if ok:
            changed.update(n for n in names.splitlines() if n.strip())

    return sorted(changed)


def run_verify_command(command: str, project_dir: str, timeout: int = 900) -> dict:
    """Run the caller's verification command (tests, lint, build).

    Uses a shell so ordinary command lines (`pytest -q && ruff check`) work.

    Two things worth being explicit about:

    - This runs on the host with your permissions, NOT inside Codex's sandbox.
      `write=False` constrains *Codex*; it does not constrain this command. The
      command comes from the caller — the same trust level as the MCP server
      itself — not from the model, so Codex cannot inject one. Don't pass a
      command you wouldn't run yourself.
    - It runs *after* file changes are computed, deliberately. Test commands
      routinely write caches (`__pycache__`, `.pytest_cache`), and attributing
      those to Codex would produce false "read-only violated" reports.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=project_dir,
            env=subprocess_env(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "exit_code": None,
            "passed": False,
            "output_tail": f"Verification command timed out after {timeout}s.",
        }
    except OSError as exc:
        return {
            "command": command,
            "exit_code": None,
            "passed": False,
            "output_tail": f"Could not run verification command: {exc}",
        }

    combined = (result.stdout + result.stderr).strip()
    tail = "\n".join(combined.splitlines()[-40:])
    return {
        "command": command,
        "exit_code": result.returncode,
        "passed": result.returncode == 0,
        "output_tail": tail,
    }


def verify(
    project_dir: str,
    before: dict,
    write: bool,
    verify_command: str = "",
    verify_timeout: int = 900,
) -> dict:
    """Compare post-task repository state against the pre-task snapshot.

    Returns a report with individual checks and an overall `verified` verdict.
    `verified` is False only when a check actively fails — a warning (e.g. a
    write task that changed nothing) is surfaced but does not fail the job,
    since some write-mode tasks legitimately conclude no change is needed.
    """
    checks = []

    if not before.get("is_repo"):
        checks.append({
            "name": "git_tracking",
            "status": "skip",
            "detail": "Not a git repository — file changes could not be verified.",
        })
        report = {"git": {"is_repo": False}, "checks": checks}
    else:
        _, head_after_raw = _git(["rev-parse", "HEAD"], project_dir)
        head_after = head_after_raw.strip()
        ok, porcelain = _git(["status", "--porcelain"], project_dir)
        after_entries = _parse_porcelain(porcelain) if ok else {}
        after_numstat = _numstat(project_dir)
        head_before = before.get("head", "")

        # "Could not inspect" must never silently become "nothing changed" —
        # that would report verified:true for a job we failed to verify at all.
        if not ok or not before.get("git_ok", True):
            checks.append({
                "name": "git_tracking",
                "status": "fail",
                "detail": (
                    "git status failed, so file changes could not be verified. "
                    "Treat this job's file claims as unchecked."
                ),
            })

        files = _changed_files(
            project_dir, before, after_entries, after_numstat,
            head_before, head_after
        )
        committed = bool(head_before and head_after and head_before != head_after)

        diff_stat = ""
        if committed:
            _, diff_stat = _git(
                ["diff", "--stat", f"{head_before}..{head_after}"], project_dir
            )
        elif files:
            _, diff_stat = _git(["diff", "--stat"], project_dir)
        diff_stat = diff_stat.strip()

        report = {
            "git": {
                "is_repo": True,
                "head_before": head_before,
                "head_after": head_after,
                "committed": committed,
                "files_changed": files,
                "diff_stat": diff_stat,
            },
            "checks": checks,
        }

        if write:
            if files:
                checks.append({
                    "name": "files_changed",
                    "status": "pass",
                    "detail": f"{len(files)} file(s) changed per git.",
                })
            else:
                checks.append({
                    "name": "files_changed",
                    "status": "warn",
                    "detail": (
                        "Task ran with write access but git shows no changes. "
                        "Either it concluded no change was needed, or it reported "
                        "work it did not do — read the output before trusting it."
                    ),
                })
        else:
            if files:
                checks.append({
                    "name": "read_only_respected",
                    "status": "fail",
                    "detail": (
                        f"Task ran read-only but {len(files)} file(s) changed: "
                        f"{', '.join(files[:10])}. Investigate before proceeding."
                    ),
                })
            else:
                checks.append({
                    "name": "read_only_respected",
                    "status": "pass",
                    "detail": "No files changed, as expected for a read-only task.",
                })

    if verify_command:
        outcome = run_verify_command(verify_command, project_dir, verify_timeout)
        checks.append({
            "name": "verify_command",
            "status": "pass" if outcome["passed"] else "fail",
            "detail": f"`{verify_command}` exited {outcome['exit_code']}.",
            "output_tail": outcome["output_tail"],
        })

    report["verified"] = not any(c["status"] == "fail" for c in checks)
    report["warnings"] = [c["detail"] for c in checks if c["status"] == "warn"]
    return report
