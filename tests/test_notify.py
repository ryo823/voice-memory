import pytest

from voice_memory import notify


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(notify.ENV, "https://discord.example/api/webhooks/1/SECRET")
    sent = []

    def post(url, text):
        sent.append((url, text))
        return True
    return tmp_path / "n.json", sent, post


def test_notify_once_then_cooldown_then_reminder(env):
    path, sent, post = env
    assert notify.notify("backup", "boom", now=1000, path=path, post=post)
    assert not notify.notify("backup", "boom", now=1000 + 3600, path=path, post=post)  # inside cooldown
    assert notify.notify("backup", "boom", now=1000 + notify.COOLDOWN + 1, path=path, post=post)
    assert len(sent) == 2 and "still failing" in sent[1][1]


def test_resolve_sends_recovery_once(env):
    path, sent, post = env
    assert not notify.resolve("backup", path=path, post=post)  # nothing was failing: silent
    notify.notify("backup", "boom", now=1, path=path, post=post)
    assert notify.resolve("backup", path=path, post=post)
    assert not notify.resolve("backup", path=path, post=post)
    assert "RECOVERED" in sent[-1][1]
    assert notify.notify("backup", "again", now=2, path=path, post=post)  # a new failure notifies immediately


def test_failed_post_is_retried_next_time(tmp_path, monkeypatch):
    monkeypatch.setenv(notify.ENV, "https://x.example/h")
    path = tmp_path / "n.json"
    assert not notify.notify("k", "m", now=1, path=path, post=lambda u, t: False)
    ok = []
    assert notify.notify("k", "m", now=2, path=path, post=lambda u, t: ok.append(t) or True) and ok


def test_no_webhook_is_silent_and_not_recorded(tmp_path, monkeypatch):
    monkeypatch.delenv(notify.ENV, raising=False)
    path = tmp_path / "n.json"
    assert not notify.notify("k", "m", path=path, post=lambda u, t: pytest.fail("must not post"))
    assert not path.exists()


def test_post_never_raises_and_never_logs_url(monkeypatch, caplog):
    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    assert notify._post("https://discord.example/api/webhooks/1/SECRET", "x") is False
    assert "SECRET" not in caplog.text


def test_clip_report(tmp_path, monkeypatch):
    from voice_memory.ingest import clip_sync

    monkeypatch.setenv(notify.ENV, "https://x.example/h")
    monkeypatch.setenv("VOICE_MEMORY_NOTIFY_STATE", str(tmp_path / "n.json"))
    sent = []
    monkeypatch.setattr(notify, "_post", lambda u, t: sent.append(t) or True)
    state = {}
    r = {"clip": None, "new": [], "done": [], "failed": []}
    clip_sync._report(r, state, now=0)  # first miss only starts the clock
    assert not sent and state["_meta"]["last_reachable"] == 0
    clip_sync._report(r, state, now=clip_sync.UNREACHABLE_AFTER + 1)
    assert len(sent) == 1 and "BLE" in sent[0]
    clip_sync._report({**r, "clip": "IDLE"}, state, now=clip_sync.UNREACHABLE_AFTER + 2)
    assert "RECOVERED clip-unreachable" in sent[-1]
    clip_sync._report({**r, "clip": "IDLE", "new": ["20261025000000"], "failed": ["20261025000000"]}, state, now=5)
    assert "FAILED clip-sync" in sent[-1] and "20261025000000" in sent[-1]
    clip_sync._report({**r, "clip": "IDLE", "new": ["20261025000000"], "done": ["20261025000000"]}, state, now=6)
    assert "RECOVERED clip-sync" in sent[-1]
