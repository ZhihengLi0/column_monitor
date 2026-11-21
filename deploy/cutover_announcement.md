:rocket: *BlueFors Monitor v5.0 — the bot has been rebuilt*

*Why:* from Nov 10 to Nov 20 the bot could not reach Slack (the guest Wi-Fi login had expired) and nobody could tell. Monitoring kept running, but every alert and reply was lost. The old setup had no way to detect, queue or recover from that.

*What is new:*
• *Never loses an alert again* — if Slack is unreachable, alerts are queued and delivered when it is back, repeats collapsed, with one short summary of what happened while offline.
• *Self-healing* — one supervised service (restarts itself in seconds), a network watchdog that re-connects the Wi-Fi, PostgreSQL waits for the data drive after a reboot, the LN2 scale reader reconnects on its own and never loses readings.
• *Faster and cleaner* — one check cycle now takes ~0.05 s (was up to minutes after a reboot); the LN2 scale is part of the same bot; logs rotate; the system disk went from 88 % to 66 %.
• *Correct local time* — plots and summaries now follow Central Time properly (the old version was one hour off since November 1).

*What stays the same:* every command (`help`, `plot MXC2 12h`, `pump status`, `ln2`, `set mode cold`, `silent for 2h` under an alert, `ack`, `pause alerts`, …), all alarm rules and thresholds, the filter and eyewash reminders, the 8 AM / 8 PM summaries.

*Small behaviour changes:*
• Commands older than 15 minutes (e.g. sent during an outage) are ignored instead of replayed.
• Replying `silent forever` only works under alerts from the last 24 h.
• "Database unreachable" is now a single alarm with a recovery message, not a message every minute.
• LN2 alarms now also post a ✅ Cleared when they recover.

Type `help` to see the full command list.
