"""SlackClient tests with `requests` stubbed out — no network."""
import time

import pytest

from bfm.core import slack as sl


class FakeResp:
    def __init__(self, payload, status=200, headers=None):
        self._p, self.status_code, self.headers = payload, status, headers or {}

    def json(self):
        return self._p


class FakeRequests:
    """Scriptable requests module: `script` is a list of responses or exceptions
    consumed in order; the last one repeats."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def _next(self):
        r = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._next()

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._next()


@pytest.fixture
def client(monkeypatch):
    def make(script):
        fake = FakeRequests(script)
        monkeypatch.setattr(sl, "requests", fake)
        c = sl.SlackClient("tok", "C1", "UBOT")
        return c, fake
    return make


def test_get_ok(client):
    c, fake = client([FakeResp({"ok": True, "messages": [{"user": "UBOT"}, {"user": "U1", "text": "yes"}]})])
    assert c.replies("1.0") == [{"user": "U1", "text": "yes"}]
    assert c.ok


def test_transport_failure_opens_circuit(client, monkeypatch):
    c, fake = client([ConnectionError("ssl hostname mismatch")])
    assert c.get("conversations.history", {}) == {}
    assert c.ok is False and c.down_since is not None
    # while down, a GET triggers at most ONE probe (api.test) per PROBE_INTERVAL
    assert c.get("reactions.get", {}) == {}
    assert [u for _, u, _ in fake.calls][-1].endswith("api.test")
    n = len(fake.calls)
    assert c.get("reactions.get", {}) == {}
    assert c.get("conversations.replies", {}) == {}
    assert len(fake.calls) == n          # refused without any request
    monkeypatch.setattr(sl, "PROBE_INTERVAL", 0)
    fake.script = [FakeResp({"ok": True})]
    assert c.probe() is True and c.ok


def test_post_now_returns_ts(client):
    c, fake = client([FakeResp({"ok": True, "ts": "1700.5"})])
    assert c.post_now("hello", color="good", thread_ts="1.1") == "1700.5"
    body = fake.calls[-1][2]["json"]
    assert body["thread_ts"] == "1.1" and body["attachments"][0]["color"] == "good"


def test_post_429_sets_retry_after(client):
    c, fake = client([FakeResp({}, status=429, headers={"Retry-After": "7"}),
                      FakeResp({"ok": True, "ts": "2"})])
    assert c.post_now("x") is None
    assert c.retry_after_until > time.time() + 5
    assert c.post_now("x") is None            # still paused, no request made
    c.retry_after_until = 0
    assert c.post_now("x") == "2"


def test_post_failure_marks_down_and_returns_none(client):
    c, fake = client([ConnectionError("boom")])
    assert c.post_now("x") is None
    assert c.ok is False


def test_dry_run_never_touches_network(client):
    c, fake = client([ConnectionError("should not be called")])
    c.dry_run = True
    assert c.post_now("x") is not None
    assert c.upload_image("/nonexistent.png", "t") is True
    assert fake.calls == []
    assert c.sent_log[0]["text"] == "x"


@pytest.mark.parametrize("raw,clean", [
    ("<tel:260622-0000|260622-0000> plot", "260622-0000 plot"),
    ("see <http://x.y|label>", "see label"),
    ("<http://x.y>", "http://x.y"),
    ("plot MXC2,12h", "plot MXC2 12h"),
    ("a &amp; b", "a & b"),
    ("  padded  ", "padded"),
])
def test_unescape(raw, clean):
    assert sl.unescape(raw) == clean
