import json
import subprocess
from types import SimpleNamespace

import pytest

from voice_memory import database as db, suggest
from test_database import _days_ago, save


def stream(ids, *, send="ok", result="RESULT: sent"):
    ev = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__voice-memory__get_pending_action_memories", "input": {}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": json.dumps({"results": [{"id": i} for i in ids]})}]}]}},
    ]
    if send:
        ev += [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t2", "name": "mcp__claude_ai_Gmail__send_message", "input": {}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "x", "is_error": send == "error"}]}},
        ]
    ev.append({"type": "result", "is_error": False, "result": result})
    return "\n".join(json.dumps(e) for e in ev)


def runner(out, rc=0):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")
    run.calls = calls
    return run


def test_sent_marks_memos_in_body(db_path):
    a = save(db_path, recorded_at=_days_ago(1))
    assert suggest.run(runner=runner(stream([])), db_path=db_path) == 1
    assert db.pending_memories(db_path=db_path, exclude_suggested=True) == []
    assert db.pending_memories(db_path=db_path)[0]["id"] == a["id"]  # still pending for /suggest-actions


def test_no_new_memos_skips_claude(db_path):
    r = runner(stream([]))
    assert suggest.run(runner=r, db_path=db_path) == 0
    assert r.calls == []


@pytest.mark.parametrize("send,result", [("error", "RESULT: sent"), (None, "RESULT: sent"), (None, "done")])
def test_unconfirmed_mail_marks_nothing(db_path, send, result):
    a = save(db_path, recorded_at=_days_ago(1))
    with pytest.raises(suggest.SuggestError):
        suggest.run(runner=runner(stream([a["id"]], send=send, result=result)), db_path=db_path)
    assert len(db.pending_memories(db_path=db_path, exclude_suggested=True)) == 1


def test_all_excluded_marks_without_mail(db_path):
    a = save(db_path, recorded_at=_days_ago(1))
    assert suggest.run(runner=runner(stream([a["id"]], send=None, result="x\nRESULT: none")), db_path=db_path) == 1


def test_dry_run_prints_db_body_without_calling_claude(db_path, capsys):
    a = save(db_path, recorded_at=_days_ago(1))
    r = runner(stream([a["id"]], send=None, result="x"))
    assert suggest.run(dry_run=True, runner=r, db_path=db_path) == 0
    assert a["id"] in capsys.readouterr().out
    assert r.calls == []
    assert len(db.pending_memories(db_path=db_path, exclude_suggested=True)) == 1


def test_nonzero_exit_marks_nothing(db_path):
    save(db_path, recorded_at=_days_ago(1))
    with pytest.raises(suggest.SuggestError):
        suggest.run(runner=runner("", rc=1), db_path=db_path)
    assert len(db.pending_memories(db_path=db_path, exclude_suggested=True)) == 1


def test_prompt_carries_body_and_only_send_tool(db_path):
    a = save(db_path, title="歯医者の予約", transcript="来週の火曜の15時に歯医者", recorded_at=_days_ago(1))
    r = runner(stream([a["id"]]))
    suggest.run(runner=r, db_path=db_path)
    cmd = r.calls[0]
    assert a["id"] in cmd[2] and "来週の火曜" in cmd[2]
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__claude_ai_Gmail__send_message"
