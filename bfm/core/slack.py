"""Slack client with health tracking, a circuit breaker on GETs, paced POSTs
and image upload — the only module that talks to slack.com.

Why: during 2026-11-10 → 11-20 every call failed (captive-portal hijack) and
the legacy code just logged and dropped 10 days of alerts; it also kept
issuing 60 GETs per minute. Here:
  * `ok` flips to False on any transport/SSL failure; while False, GETs are
    refused (one `probe()` per cycle re-tests), so a dead network costs one
    request per minute, not sixty 10-second timeouts.
  * POSTs are only ever issued by the daemon's sender thread draining the
    StateStore outbox (see daemon.py); `post_now()` is the primitive.
  * HTTP 429 is honoured (Retry-After); other failures back off exponentially.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

log = logging.getLogger("bfm.slack")

API = "https://slack.com/api/"
GET_TIMEOUT = 10
POST_TIMEOUT = 10
UPLOAD_TIMEOUT = 60
PROBE_INTERVAL = 55        # seconds between probes while down


class SlackClient:
    def __init__(self, token: str, channel: str, bot_user_id: str, *, dry_run: bool = False):
        self.token = token
        self.channel = channel
        self.bot_user_id = bot_user_id
        self.dry_run = dry_run
        self.ok = True                     # health flag (transport level)
        self.down_since: float | None = None
        self.last_probe = 0.0
        self.last_error: str | None = None
        self.retry_after_until = 0.0
        self.sent_log: list[dict] = []     # dry-run: what would have been posted

    # ── health ───────────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _mark_down(self, err: Exception | str) -> None:
        self.last_error = str(err)[:300]
        if self.ok:
            log.error("Slack unreachable: %s", self.last_error)
            self.ok = False
            self.down_since = time.time()

    def _mark_up(self) -> None:
        if not self.ok:
            secs = time.time() - (self.down_since or time.time())
            log.info("Slack reachable again after %.0f min", secs / 60)
        self.ok = True
        self.down_since = None
        self.last_error = None

    def probe(self, force: bool = False) -> bool:
        """Cheap reachability test (api.test, no auth needed). Rate-limited while
        down so a dead network costs one request per PROBE_INTERVAL."""
        if self.ok and not force:
            return True
        if not force and time.time() - self.last_probe < PROBE_INTERVAL:
            return False
        self.last_probe = time.time()
        try:
            r = requests.get(API + "api.test", timeout=GET_TIMEOUT)
            if r.json().get("ok"):
                self._mark_up()
                return True
            self._mark_down(f"api.test: {r.status_code}")
        except Exception as e:
            self._mark_down(e)
        return False

    # ── GET ──────────────────────────────────────────────────────────────────
    def get(self, endpoint: str, params: dict) -> dict:
        """Slack Web API GET. Returns {} when unreachable (and refuses to try
        while the circuit is open)."""
        if not self.ok and not self.probe():
            return {}
        try:
            r = requests.get(API + endpoint, params=params, headers=self._headers(),
                             timeout=GET_TIMEOUT)
            data = r.json()
            self._mark_up()
            if not data.get("ok"):
                log.warning("Slack GET %s: %s", endpoint, data.get("error"))
            return data
        except Exception as e:
            self._mark_down(e)
            return {}

    def replies(self, ts: str) -> list[dict]:
        """Human replies under a message (bot's own replies filtered out)."""
        data = self.get("conversations.replies", {"channel": self.channel, "ts": ts})
        if not data.get("ok"):
            return []
        return [m for m in data.get("messages", [])[1:]
                if m.get("user") != self.bot_user_id]

    def reactions(self, ts: str) -> set[str]:
        data = self.get("reactions.get", {"channel": self.channel, "timestamp": ts})
        if not data.get("ok"):
            return set()
        return {r.get("name") for r in data.get("message", {}).get("reactions", [])}

    def history(self, oldest: str, limit: int = 50) -> list[dict]:
        data = self.get("conversations.history",
                        {"channel": self.channel, "oldest": oldest, "limit": limit})
        if not data.get("ok"):
            return []
        return data.get("messages", [])

    # ── POST ─────────────────────────────────────────────────────────────────
    def post_now(self, text: str, *, color: str = "danger", thread_ts: str | None = None,
                 footer: str | None = None) -> str | None:
        """chat.postMessage immediately. Returns ts, or None on failure.
        Honours a pending Retry-After. Callers normally go through the outbox."""
        if self.dry_run:
            self.sent_log.append({"text": text, "color": color, "thread_ts": thread_ts})
            log.info("[dry-run] would post%s: %s", f" (thread {thread_ts})" if thread_ts else "",
                     text.replace("\n", " | ")[:200])
            return f"{time.time():.6f}"
        if time.time() < self.retry_after_until:
            return None
        payload = {
            "channel": self.channel,
            "attachments": [{"color": color, "text": text, "mrkdwn_in": ["text"],
                             "footer": footer or f"BlueFors Monitor | {time.strftime('%Y-%m-%d %H:%M:%S')}"}],
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        try:
            r = requests.post(API + "chat.postMessage", json=payload,
                              headers=self._headers(), timeout=POST_TIMEOUT)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "5"))
                self.retry_after_until = time.time() + wait
                log.warning("Slack 429 — pausing posts for %ds", wait)
                return None
            data = r.json()
            self._mark_up()
            if data.get("ok"):
                return data.get("ts")
            log.error("Slack post error: %s", data.get("error"))
            return None
        except Exception as e:
            self._mark_down(e)
            return None

    def delete(self, ts: str) -> bool:
        if self.dry_run:
            return True
        try:
            r = requests.post(API + "chat.delete", json={"channel": self.channel, "ts": ts},
                              headers=self._headers(), timeout=POST_TIMEOUT)
            return bool(r.json().get("ok"))
        except Exception as e:
            self._mark_down(e)
            return False

    # ── image upload (3-step external upload) ────────────────────────────────
    def upload_image(self, path: str | Path, title: str, comment: str = "",
                     thread_ts: str | None = None, attempts: int = 3) -> bool:
        if self.dry_run:
            self.sent_log.append({"upload": str(path), "title": title, "thread_ts": thread_ts})
            log.info("[dry-run] would upload %s (%s)", path, title)
            return True
        path = Path(path)
        for attempt in range(1, attempts + 1):
            try:
                if self._upload_once(path, title, comment, thread_ts):
                    self._mark_up()
                    return True
            except Exception as e:
                log.warning("upload attempt %d/%d failed: %s", attempt, attempts, e)
                self._mark_down(e)
            time.sleep(2 * attempt)
        return False

    def _upload_once(self, path: Path, title: str, comment: str, thread_ts: str | None) -> bool:
        size = path.stat().st_size
        r = requests.get(API + "files.getUploadURLExternal",
                         params={"filename": path.name, "length": size},
                         headers=self._headers(), timeout=15).json()
        if not r.get("ok"):
            log.error("getUploadURLExternal: %s", r.get("error"))
            return False
        with path.open("rb") as f:
            up = requests.post(r["upload_url"], files={"file": (path.name, f, "image/png")},
                               timeout=UPLOAD_TIMEOUT)
        if up.status_code != 200:
            log.error("upload POST status %s", up.status_code)
            return False
        body = {"files": [{"id": r["file_id"], "title": title}],
                "channel_id": self.channel, "initial_comment": comment}
        if thread_ts:
            body["thread_ts"] = thread_ts
        done = requests.post(API + "files.completeUploadExternal", json=body,
                             headers=self._headers(), timeout=15).json()
        if not done.get("ok"):
            log.error("completeUploadExternal: %s", done.get("error"))
            return False
        return True


def unescape(text: str) -> str:
    """Strip Slack auto-markup (<tel:...|...>, <http://x|label>, &amp;) and the
    commas people type between tokens ('plot MXC2,12h')."""
    import re
    t = re.sub(r"<tel:([^|>]+)\|[^>]*>", r"\1", text or "")   # <tel:VALUE|DISPLAY> → VALUE
    t = re.sub(r"<https?://[^|>]+\|([^>]+)>", r"\1", t)        # <URL|label> → label
    t = re.sub(r"<(https?://[^>]+)>", r"\1", t)                # <URL> → URL
    t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    t = re.sub(r"\s*,\s*", " ", t)                             # commas → spaces
    return t.strip()
