#!/usr/bin/env python3
"""Lean daily screen (8:00 AM ET): find up to 15 qualifying pregame favorites. PAPER ONLY. No bot wake."""
import json
import sys
import time
from datetime import datetime

import lean_lib as L

LOCK = 'lean_screen.lock'
LOG = 'logs/lean_screen.log'


def main():
    lock = L.Lock(LOCK)
    if not lock.acquire():
        status = dict(event='lean_screen', ok=False, reason='locked', run_et=L.now_et())
        print(json.dumps(status))
        L.log_line(LOG, 'skip: lock held')
        return 0

    now = time.time()
    L.log_line(LOG, 'start screen')
    try:
        games, cover, n_cand = L.scan_candidate_markets(horizon_hours=36, lookback_hours=2)
    except Exception as e:
        L.log_line(LOG, f'error scan: {e}')
        print(json.dumps(dict(event='lean_screen', ok=False, error=str(e), run_et=L.now_et())))
        lock.release()
        return 1

    # Qualifying: not yet started, liquid book, favorite mid >= 90%
    qual = []
    for key, g in games.items():
        if g['start'] <= now:
            continue
        if not g['ok'] or g['bb'] is None or g['ba'] is None:
            continue
        try:
            mids = {o: L.side(g, o)[0] for o in g['oc']}
        except Exception:
            continue
        fav = max(mids, key=mids.get)
        pre = mids[fav]
        if pre < L.PREGAME_MIN:
            continue
        fav_i = g['oc'].index(fav)
        token = g['tokens'][fav_i] if len(g['tokens']) == 2 else None
        qual.append(dict(
            key=key,
            market_id=g['mid_id'],
            tokens=g['tokens'],
            token=token,
            fav=fav,
            pregame=round(pre, 4),
            start=g['start'],
            start_et=datetime.fromtimestamp(g['start'], L.ET).strftime('%Y-%m-%d %H:%M:%S %Z'),
            url='https://polymarket.com/event/' + g['eslug'],
            league=g['league'],
            title=g['title'],
            outcomes=g['oc'],
            eslug=g['eslug'],
            screened_et=L.now_et(),
            screened_at=now,
            bet=False,
            settled=False,
            recorded=False,  # official pregame within 30m of start not yet locked
        ))

    # Prefer soonest starts; cap at MAX_WATCH
    qual.sort(key=lambda x: x['start'])
    watch = qual[: L.MAX_WATCH]

    # Preserve bet/recorded flags if same market still on watch from prior day
    prev = L.load_json(L.WATCH_PATH, {'games': []})
    prev_by_key = {w.get('key'): w for w in prev.get('games', []) if w.get('key')}
    for w in watch:
        old = prev_by_key.get(w['key'])
        if not old:
            continue
        if old.get('bet'):
            w['bet'] = True
        if old.get('recorded') and old.get('pregame') is not None:
            w['recorded'] = True
            w['pregame'] = old['pregame']
            w['fav'] = old.get('fav', w['fav'])
            w['token'] = old.get('token', w['token'])
            w['recorded_et'] = old.get('recorded_et')
            w['recorded_at'] = old.get('recorded_at')

    # Keep games already in progress from the prior list (recorded or bet, not settled)
    keys = {w['key'] for w in watch}
    for old in prev.get('games', []):
        if old.get('key') in keys or old.get('settled'):
            continue
        if float(old.get('start') or 0) <= now and (old.get('recorded') or old.get('bet')):
            watch.append(old)

    by_league = {}
    for w in watch:
        by_league[w['league']] = by_league.get(w['league'], 0) + 1

    out = dict(
        updated_et=L.now_et(),
        updated_at=now,
        count=len(watch),
        by_league=by_league,
        games=watch,
    )
    L.save_json(L.WATCH_PATH, out)
    status = dict(
        event='lean_screen',
        ok=True,
        paper=True,
        run_et=L.now_et(),
        markets_scanned=n_cand,
        games_scanned=len(games),
        games_by_league_scanned=cover,
        qualifying_found=len(qual),
        saved=len(watch),
        by_league=by_league,
        watch_path=L.WATCH_PATH,
    )
    print(json.dumps(status))
    L.log_line(LOG, f"done saved={len(watch)} qual={len(qual)} by_league={by_league}")
    lock.release()
    return 0


if __name__ == '__main__':
    sys.exit(main())
