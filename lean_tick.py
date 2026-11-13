#!/usr/bin/env python3
"""One tick for the game-time routine: run lean_check only
if a watched game starts within 10 min or is live (recorded/bet, unsettled). PAPER ONLY."""
import json, subprocess, sys, time, os
from datetime import datetime

DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(DIR)
os.environ['TZ'] = 'America/New_York'
try:
    time.tzset()
except AttributeError:
    pass
os.makedirs('logs', exist_ok=True)
today = datetime.now().strftime('%Y-%m-%d')
try:
    w = json.load(open('lean_watch.json'))
except Exception:
    w = {}
now = time.time()
active = 0
for g in w.get('games', []):
    if g.get('settled'):
        continue
    st = float(g.get('start') or 0)
    if 0 < st - now <= 600:
        active += 1
    elif st <= now and (g.get('recorded') or g.get('bet')):
        active += 1
rc = 0
if active:
    with open('logs/lean_check.log', 'a') as logf:
        rc = subprocess.run(
            [sys.executable, 'lean_check.py'],
            stdout=logf,
            stderr=subprocess.STDOUT,
        ).returncode
print(f"tick {datetime.now():%H:%M} active={active}")
if rc != 0:
    sys.exit(rc)
