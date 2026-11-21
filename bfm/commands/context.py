"""CommandContext — what a command handler gets to work with."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..core.db import Database, DataContext, DatabaseUnavailable

log = logging.getLogger("bfm.commands")


@dataclass
class CommandContext:
    st: dict                       # the live state dict (caller holds the lock)
    cs2: Database | None
    ln2: Database | None
    enqueue: Callable[..., dict]   # StateStore.enqueue-compatible
    upload: Callable[..., bool]    # (path, title, thread_ts=...) -> bool
    reply_ts: str
    user_id: str = "default"
    raw_text: str = ""
    _data: DataContext | None = field(default=None, repr=False)

    # ── replies ──────────────────────────────────────────────────────────────
    def reply(self, text: str, color: str = "#0066cc", *, top_level: bool = False) -> dict:
        return self.enqueue(text, color=color,
                            thread_ts=None if top_level else self.reply_ts, kind="reply")

    def reply_error(self, text: str) -> dict:
        return self.reply(text, color="danger")

    # ── data access ──────────────────────────────────────────────────────────
    @property
    def data(self) -> DataContext:
        if self._data is None:
            if self.cs2 is None:
                raise DatabaseUnavailable("cs2: no database connection")
            self._data = DataContext(self.cs2, self.ln2)
        return self._data

    def db_ok(self, what: str = "do that") -> bool:
        """Reply with the legacy 'no database connection' line if the fridge DB
        is unavailable; returns True when it can be used."""
        if self.cs2 is None or not self.cs2.available:
            self.reply(f"Cannot {what}: no database connection.")
            return False
        return True

    def ln2_ok(self) -> bool:
        return self.ln2 is not None and self.ln2.available
