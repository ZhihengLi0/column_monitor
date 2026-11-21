"""Slack @mention command layer.

router.py   ordered rule table → handler (same precedence as the legacy
            574-line _execute_command, now one rule per line)
context.py  CommandContext: state, data access, reply()/upload()
readings.py pressure / temperature / pump / heater / pulse-tube / valve status
plot.py     one renderer for every plot (sensors, multi-sensor, pulse-tube, LN2)
status.py   help, list, status, alarm listing
control.py  pause/resume, sentinel, set mode, ack, change/reset threshold, filter
ln2.py      liquid-nitrogen scale queries + silence
nlp.py      natural-language fallback and the self-learning feedback loop
"""
from .router import Router  # noqa: F401
