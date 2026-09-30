"""Tests for the app-server transport, against a scripted fake `codex app-server`."""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app_server
from codex_runner import CodexError

# A stand-in for `codex app-server`: newline-delimited JSON-RPC on stdio. It
# logs every message it receives to FAKE_LOG and behaves per FAKE_MODE.
_FAKE_SERVER = r'''
import json, os, sys, time

mode = os.environ.get("FAKE_MODE", "ok")
log = open(os.environ["FAKE_LOG"], "a")
rollout = os.environ["FAKE_ROLLOUT"]
THREAD, TURN = "thread-1", "turn-1"

def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()

def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})

def finish(status="completed", error=None):
    notify("turn/completed", {"threadId": THREAD, "turn": {
        "id": TURN, "status": status, "error": error, "items": []}})

for line in sys.stdin:
    message = json.loads(line)
    log.write(line)
    log.flush()
    method, rid = message.get("method"), message.get("id")
    if rid == 99 and method is None:
        # Our reply to the approval request below; the turn waits on it.
        if "error" in message:
            finish()
        continue
    if rid is None:
        continue  # a notification (initialized)
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method in ("thread/start", "thread/resume"):
        THREAD = message["params"].get("threadId", THREAD)
        send({"jsonrpc": "2.0", "id": rid,
              "result": {"thread": {"id": THREAD, "path": rollout}}})
    elif method == "turn/start":
        if mode == "rpc_error":
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32602, "message": "unknown field cyberAccessProgram"}})
            continue
        if mode == "completed_before_response":
            notify("item/completed", {"threadId": THREAD, "item": {
                "type": "agentMessage", "text": "early"}})
            finish()
            send({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": TURN}}})
            continue
        send({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": TURN}}})
        if mode == "hang":
            notify("item/completed", {"threadId": THREAD, "item": {
                "type": "agentMessage", "text": "Reading the code first."}})
            time.sleep(60)
        if mode == "exit_mid_turn":
            sys.exit(3)
        if mode == "server_request":
            send({"jsonrpc": "2.0", "id": 99, "method": "item/commandExecution/requestApproval",
                  "params": {"threadId": THREAD}})
            continue
        if mode == "daybreak_refused":
            detail = ("unexpected status 403 Forbidden: {\"detail\":\"Daybreak isn't "
                      "available for this model. Turn off Daybreak or choose another model.\"}")
            notify("error", {"threadId": THREAD, "willRetry": True,
                             "error": {"message": "Reconnecting... 1/5"}})
            notify("error", {"threadId": THREAD, "willRetry": False,
                             "error": {"message": detail}})
            finish("failed", {"message": detail})
            continue
        # A sub-agent's thread: none of this is ours.
        notify("item/completed", {"threadId": "sub-agent", "item": {
            "type": "agentMessage", "text": "sub-agent chatter"}})
        notify("turn/completed", {"threadId": "sub-agent", "turn": {
            "id": TURN, "status": "failed", "error": {"message": "not ours"}}})
        notify("item/completed", {"threadId": THREAD, "item": {
            "type": "commandExecution", "command": "pytest -q", "exitCode": 0}})
        notify("thread/tokenUsage/updated", {"threadId": THREAD, "tokenUsage": {"total": {
            "inputTokens": 10, "cachedInputTokens": 4, "cacheWriteInputTokens": 3,
            "outputTokens": 2, "reasoningOutputTokens": 1, "totalTokens": 12}}})
        notify("item/completed", {"threadId": THREAD, "item": {
            "type": "agentMessage", "text": '{"answer": "OK"}'}})
        finish()
        if mode == "slow_exit":
            time.sleep(60)  # ignores stdin EOF, as a slow shutdown would
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_codex"
    script.write_text(f"#!{sys.executable}\n{_FAKE_SERVER}")
    script.chmod(0o755)
    log = tmp_path / "requests.jsonl"
    monkeypatch.setattr(app_server, "find_codex_binary", lambda: str(script))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_ROLLOUT", str(tmp_path / "rollout.jsonl"))

    def run(mode="ok", timeout=30, **kwargs):
        monkeypatch.setenv("FAKE_MODE", mode)
        events = []
        result = app_server.run_codex_app_server(
            project_dir=str(tmp_path), prompt="do x", model="gpt-6-sol",
            effort="high", sandbox="read-only",
            output_file=str(tmp_path / "o.txt"),
            prompt_file=str(tmp_path / "p.txt"),
            stderr_file=str(tmp_path / "e.txt"),
            timeout=timeout, cyber_access_program="daybreak_blue",
            on_event=lambda event, state: events.append(event),
            **kwargs,
        )
        return result, events

    def requests():
        return {m["method"]: m.get("params") for m in map(json.loads, log.read_text().splitlines())}

    run.requests = requests
    return run


class TestSuccessfulTurn:
    def test_returns_run_codex_contract(self, fake, tmp_path):
        result, _ = fake()
        assert result["thread_id"] == "thread-1"
        assert result["output"] == '{"answer": "OK"}'
        assert result["timed_out"] is False
        assert result["exit_code"] == 0
        assert result["rollout_path"] == str(tmp_path / "rollout.jsonl")
        assert (tmp_path / "o.txt").read_text() == '{"answer": "OK"}'

    def test_usage_uses_exec_spelling(self, fake):
        result, _ = fake()
        assert result["usage"] == {"input_tokens": 10, "cached_input_tokens": 4,
                                   "cache_write_input_tokens": 3, "output_tokens": 2,
                                   "reasoning_output_tokens": 1}

    def test_events_are_translated_to_exec_shape(self, fake):
        # The worker logs commands and tracks phases off exec-shaped events;
        # it must not need to know which transport ran.
        _, events = fake()
        assert events[0] == {"type": "thread.started", "thread_id": "thread-1"}
        assert {"type": "item.completed",
                "item": {"type": "command_execution", "command": "pytest -q",
                         "exit_code": 0}} in events
        assert events[-1]["type"] == "turn.completed"

    def test_other_threads_are_ignored(self, fake):
        # `ultra` sub-agents run in threads of their own. Their messages are
        # not the answer, and their turns finishing does not finish ours.
        result, events = fake()
        assert "sub-agent" not in result["output"]
        assert not any(e["type"] == "turn.failed" for e in events)


class TestRequests:
    def test_turn_requests_the_program_in_protocol_spelling(self, fake):
        fake()
        turn = fake.requests()["turn/start"]
        assert turn["cyberAccessProgram"] == "daybreakBlue"
        assert turn["model"] == "gpt-6-sol" and turn["effort"] == "high"
        assert turn["input"] == [{"type": "text", "text": "do x"}]

    def test_opts_into_the_experimental_api(self, fake):
        # cyberAccessProgram does not exist without it.
        fake()
        init = fake.requests()["initialize"]
        assert init["capabilities"]["experimentalApi"] is True

    def test_fresh_thread_is_non_interactive_in_the_sandbox(self, fake, tmp_path):
        fake()
        thread = fake.requests()["thread/start"]
        assert thread["sandbox"] == "read-only"
        assert thread["approvalPolicy"] == "never"
        assert thread["cwd"] == str(tmp_path)

    def test_resume_uses_thread_resume(self, fake):
        result, _ = fake(resume_thread_id="old-thread")
        assert fake.requests()["thread/resume"]["threadId"] == "old-thread"
        assert "thread/start" not in fake.requests()
        assert result["thread_id"] == "old-thread"
        assert result["timed_out"] is False
        assert result["output"] == '{"answer": "OK"}'

    def test_output_schema_is_sent_inline(self, fake, tmp_path):
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        schema_file = tmp_path / "schema.json"
        schema_file.write_text(json.dumps(schema))
        result, _ = fake(schema_file=str(schema_file))
        assert fake.requests()["turn/start"]["outputSchema"] == schema
        assert result["structured_output"] == {"answer": "OK"}


class TestFailures:
    def test_daybreak_refusal_is_classified(self, fake):
        with pytest.raises(CodexError, match="refused Daybreak"):
            fake("daybreak_refused")

    def test_a_turn_finished_before_its_response_is_not_missed(self, fake):
        # A fast failure can complete the turn before turn/start's response;
        # waiting for a completion that already happened would hang to timeout.
        result, _ = fake("completed_before_response", timeout=10)
        assert result["timed_out"] is False
        assert result["output"] == "early"

    def test_protocol_rejection_fails_loudly(self, fake):
        # How a renamed field shows up after a CLI upgrade.
        with pytest.raises(CodexError, match="rejected turn/start"):
            fake("rpc_error")

    def test_server_requests_are_declined_not_left_hanging(self, fake):
        # The fake finishes the turn only once it has our reply; an unanswered
        # request would run to the timeout.
        result, _ = fake("server_request", timeout=10)
        assert result["timed_out"] is False
        assert result["exit_code"] == 0

    def test_exit_mid_turn_is_a_failure(self, fake):
        with pytest.raises(CodexError, match="exit 3"):
            fake("exit_mid_turn")

    def test_timeout_is_enforced(self, fake):
        started = time.monotonic()
        result, _ = fake("hang", timeout=2)
        assert result["timed_out"] is True
        assert time.monotonic() - started < 20

    def test_interim_commentary_is_not_reported_as_output(self, fake):
        # exec's -o holds only a finished turn's answer; a cut-off turn's last
        # message is progress narration, not a result.
        result, _ = fake("hang", timeout=2)
        assert "Reading the code first." not in result["output"]
        assert "timed out" in result["output"]

    def test_a_slow_shutdown_after_the_turn_is_not_a_timeout(self, fake):
        # The deadline passing while app-server shuts down cuts nothing short.
        result, _ = fake("slow_exit", timeout=2)
        assert result["timed_out"] is False
        assert result["output"] == '{"answer": "OK"}'
