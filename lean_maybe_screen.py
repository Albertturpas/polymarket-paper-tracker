#!/usr/bin/env python3
"""Run the daily screen once per America/New_York evening, or if the watch list is stale.

PAPER ONLY. This does not change buy or settle rules. lean_screen.py already
keeps in-progress games (recorded or bet, not settled) on the watch list.

A small screen_state.json records the ET date of the last successful evening
screen (the 10:00 PM run that builds the next day's list) so later ticks the
same night do not screen again. A daytime refresh because the watch list is
missing or older than 24 hours does not consume that evening slot.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

import lean_lib as L

STATE_PATH = 'screen_state.json'
STALE_SEC = 24 * 3600
EVENING_HOUR = 22  # 10:00 PM America/New_York


def watch_is_stale():
    """True when lean_watch.json is missing, unreadable, or updated_at is >= 24h old.

    Uses the timestamp inside the file, not filesystem mtime: a git checkout
    rewrites mtime on every Actions run.
    """
    if not os.path.exists(L.WATCH_PATH):
        return True, 'watch list missing'
    doc = L.load_json(L.WATCH_PATH, None)
    if not isinstance(doc, dict):
        return True, 'watch list unreadable'
    try:
        updated = float(doc.get('updated_at') or 0)
    except (TypeError, ValueError):
        updated = 0.0
    if updated <= 0:
        return True, 'watch list has no updated_at'
    age = time.time() - updated
    if age >= STALE_SEC:
        return True, f'watch list age {age / 3600:.1f}h >= 24h'
    return False, ''


def evening_due(now_et, state):
    """True at or after 10:00 PM ET when this ET date has not been screened yet."""
    if now_et.hour < EVENING_HOUR:
        return False, ''
    today = now_et.date().isoformat()
    last = str((state or {}).get('last_evening_screen_et_date') or '')
    if last == today:
        return False, ''
    return True, f'evening screen for the next day (last={last or "never"}, today={today})'


def main():
    now_et = datetime.now(L.ET)
    state = L.load_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    reasons = []
    stale, why_stale = watch_is_stale()
    if stale:
        reasons.append(why_stale)
    due, why_evening = evening_due(now_et, state)
    if due:
        reasons.append(why_evening)
    if not reasons:
        print(f'screen: skip ({now_et.strftime("%Y-%m-%d %H:%M %Z")})')
        return 0

    reason = '; '.join(reasons)
    print(f'screen: run ({reason})')
    rc = subprocess.run([sys.executable, os.path.join(L.DIR, 'lean_screen.py')]).returncode
    if rc != 0:
        # Leave the evening date unset so the next tick retries. The live
        # tick still runs; in-progress games stay on the current watch list.
        print(f'screen: lean_screen.py exited {rc}; not recording the screen date')
        return 0

    state['last_screen_et'] = L.now_et()
    state['last_screen_reason'] = reason
    if due:
        state['last_evening_screen_et_date'] = now_et.date().isoformat()
    L.save_json(STATE_PATH, state)
    print('screen: ok ' + json.dumps(state))
    return 0


if __name__ == '__main__':
    sys.exit(main())
