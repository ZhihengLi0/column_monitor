"""bfm — BlueFors + LN2 lab monitor (single-daemon rewrite, 2026-11).

Package layout:
  core/      state store, database access, Slack client, units, time helpers
  alarms.py  condition alarms (threshold / staleness / device health / ...)
  events.py  cursor-based event watchers (CS2 alerts, pumps, heaters, valves)
  reminders.py  recurring maintenance reminders (filter, eyewash)
  commands/  Slack @mention command router and handlers
  summary.py 12-hour summary
  daemon.py  the long-running service (check / command / sender threads)
  cli.py     --once / --summary / --init / --dry-run / --status
"""

__version__ = "5.0.0"
