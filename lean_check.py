#!/usr/bin/env python3
"""Lean live checker (every 5 min): record pregame, paper-buy dips, settle. PAPER ONLY. No bot wake."""
import json
import sys
import time
from datetime import datetime

import lean_lib as L

LOCK = 'lean_check.lock'
LOG = 'logs/lean_check.log'


def ensure_state_entry(S, w, now):
    """Mirror watch game into paper_state.json games map (paper.py schema)."""
    k = w['key']
    g = S.setdefault('games', {}).get(k)
    if g is None:
        S['games'][k] = dict(
            title=w['title'],
            league=w['league'],
            start=w['start'],
            market_id=w['market_id'],
            fav=w['fav'],
            token=w.get('token'),
            pregame=w.get('pregame'),
            recorded=w.get('recorded_at') or now,
            recorded_et=w.get('recorded_et') or L.now_et(),
            url=w['url'],
            bet=bool(w.get('bet')),
            settled=bool(w.get('settled')),
            below_since=None,
        )
        return S['games'][k]
    # keep fields in sync when watch has fresher official pregame
    g['title'] = w.get('title', g.get('title'))
    g['league'] = w.get('league', g.get('league'))
    g['start'] = w.get('start', g.get('start'))
    g['market_id'] = w.get('market_id', g.get('market_id'))
    g['url'] = w.get('url', g.get('url'))
    if w.get('recorded') and w.get('pregame') is not None:
        g['fav'] = w['fav']
        g['pregame'] = w['pregame']
        g['token'] = w.get('token', g.get('token'))
        g['recorded'] = w.get('recorded_at', g.get('recorded'))
        g['recorded_et'] = w.get('recorded_et', g.get('recorded_et'))
    return g


def place_paper_buy(S, w, s, ask, now):
    """One simulated $5 buy at best ask; append CSV + new_bets.jsonl."""
    sh = L.STAKE / ask
    s.update(bet=True, fill=round(ask, 4), shares=sh, bet_et=L.now_et())
    w['bet'] = True
    row = dict(
        date_et=L.now_et(),
        league=s['league'],
        game=s['title'],
        favorite=s['fav'],
        pregame_pct=round(100 * float(s['pregame']), 1),
        fill_price=round(ask, 4),
        staked_usd=L.STAKE,
        status='open',
        pnl_usd='',
        market_url=s['url'],
    )
    L.append_trade(row)
    L.append_new_bet_event(row)
    return dict(event='new_trade', paper=True, **row)


def main():
    lock = L.Lock(LOCK)
    if not lock.acquire():
        status = dict(event='lean_check', ok=False, reason='locked', run_et=L.now_et())
        print(json.dumps(status))
        L.log_line(LOG, 'skip: lock held')
        return 0

    now = time.time()
    L.log_line(LOG, 'start check')
    watch_doc = L.load_json(L.WATCH_PATH, {'games': []})
    watch = list(watch_doc.get('games') or [])
    S = L.load_json(L.STATE_PATH, {'games': {}})
    if 'games' not in S:
        S['games'] = {}
    L.ensure_csv()

    events = []
    new_bets = 0
    recorded_n = 0
    live_checked = 0
    errors = []

    # 1) Official pregame within ~30 min of start (forward-looking only)
    for w in watch:
        if w.get('bet') or w.get('settled') or w.get('recorded'):
            continue
        start = float(w.get('start') or 0)
        if start <= now or start - now > L.PREGAME_WINDOW_SEC:
            continue
        try:
            g = L.fetch_market_by_id(w['market_id'])
        except Exception as e:
            errors.append(f"pregame fetch {w.get('key')}: {e}")
            continue
        if not g or not g.get('ok') or g['bb'] is None or g['ba'] is None:
            continue
        # prefer outcomes from live fetch; fall back to watch
        if g.get('league') == 'UNK':
            g['league'] = w.get('league')
        try:
            mids = {o: L.side(g, o)[0] for o in g['oc']}
        except Exception:
            continue
        fav = max(mids, key=mids.get)
        pre = mids[fav]
        fav_i = g['oc'].index(fav)
        token = g['tokens'][fav_i] if len(g['tokens']) == 2 else w.get('token')
        w.update(
            fav=fav,
            pregame=round(pre, 4),
            token=token,
            tokens=g['tokens'] or w.get('tokens'),
            outcomes=g['oc'],
            recorded=True,
            recorded_at=now,
            recorded_et=L.now_et(),
            title=g.get('title') or w.get('title'),
            league=g.get('league') or w.get('league'),
            start=g.get('start') or w.get('start'),
        )
        ensure_state_entry(S, w, now)
        # Only keep as strategy candidate if official pregame >= 90%
        if pre < L.PREGAME_MIN:
            w['qualifies'] = False
        else:
            w['qualifies'] = True
        recorded_n += 1

    # 2) Live check: watched games that started, not bet, not settled
    for w in watch:
        if w.get('bet') or w.get('settled'):
            continue
        start = float(w.get('start') or 0)
        if start > now:
            continue
        # Need an official pregame record within 30m of start and >= 90%
        s = ensure_state_entry(S, w, now)
        # If never officially recorded, try one last record from current book only if start was <30m ago
        # and we still have watch pregame from screen — but strategy requires record within 30m of start.
        if not w.get('recorded'):
            # Attempt late official record only if still within 30m after start? paper.py records BEFORE start.
            # If we missed the window, do not invent a bet from screen-only quote after start.
            continue
        if float(s.get('pregame') or 0) < L.PREGAME_MIN:
            continue
        # paper.py: skip if recorded too early (>30m before start)
        if s.get('start', 0) - float(s.get('recorded') or 0) > L.PREGAME_WINDOW_SEC:
            continue

        try:
            g = L.fetch_market_by_id(w['market_id'])
        except Exception as e:
            errors.append(f"live fetch {w.get('key')}: {e}")
            continue
        if not g:
            continue
        if g.get('closed'):
            # market closed without a bet — stop tracking
            w['settled'] = True
            continue
        if g['bb'] is None or g['ba'] is None:
            continue
        # align outcomes with state fav
        g['oc'] = g.get('oc') or w.get('outcomes')
        if s['fav'] not in g['oc']:
            continue
        live_checked += 1
        try:
            cur, ask = L.side(g, s['fav'])
        except Exception:
            continue
        if cur > L.BUY_MAX:
            s['below_since'] = None
            continue
        if s.get('below_since') is None:
            s['below_since'] = now
        # placeholder artifact guard (same as paper.py)
        if now - start < 180 and 0.45 <= cur <= 0.55:
            continue
        if not g.get('ok') or not (0 < ask < 1):
            continue
        # Forward-looking paper buy
        ev = place_paper_buy(S, w, s, ask, now)
        events.append(ev)
        new_bets += 1
        L.log_line(LOG, f"NEW_BET {ev['game']} fill={ev['fill_price']} pregame={ev['pregame_pct']}")

    # 3) Also check paper_state live entries that may not be on today's watch
    #    (open watching from prior screen) — only for keys already in state with pregame>=0.9
    watch_keys = {w['key'] for w in watch if w.get('key')}
    for k, s in list(S.get('games', {}).items()):
        if k in watch_keys:
            continue
        if s.get('bet') or s.get('settled'):
            continue
        if float(s.get('pregame') or 0) < L.PREGAME_MIN:
            continue
        if s.get('start', 0) > now:
            continue
        if s.get('start', 0) - float(s.get('recorded') or 0) > L.PREGAME_WINDOW_SEC:
            continue
        # Only consider recent starts (same day-ish) to stay cheap
        if now - float(s.get('start') or 0) > 12 * 3600:
            continue
        try:
            g = L.fetch_market_by_id(s['market_id'])
        except Exception as e:
            errors.append(f"state live fetch {k}: {e}")
            continue
        if not g or g.get('closed') or g['bb'] is None or g['ba'] is None:
            continue
        if s['fav'] not in g['oc']:
            continue
        live_checked += 1
        try:
            cur, ask = L.side(g, s['fav'])
        except Exception:
            continue
        if cur > L.BUY_MAX:
            s['below_since'] = None
            continue
        if s.get('below_since') is None:
            s['below_since'] = now
        if now - float(s['start']) < 180 and 0.45 <= cur <= 0.55:
            continue
        if not g.get('ok') or not (0 < ask < 1):
            continue
        # synthesize a minimal watch-like dict for place_paper_buy flag sync
        wfake = {'key': k, 'bet': False}
        ev = place_paper_buy(S, wfake, s, ask, now)
        events.append(ev)
        new_bets += 1
        L.log_line(LOG, f"NEW_BET(state) {ev['game']} fill={ev['fill_price']}")

    # 4) Settlement
    try:
        settlements = L.settle_open(S, now)
        events.extend(settlements)
        for ev in settlements:
            L.log_line(LOG, f"SETTLE {ev['game']} {ev['status']} pnl={ev['pnl_usd']}")
    except Exception as e:
        errors.append(f'settle: {e}')

    # 5) Prune watch: drop ended/settled/old; keep upcoming + live open
    kept = []
    for w in watch:
        start = float(w.get('start') or 0)
        if w.get('settled'):
            continue
        # drop if started long ago and either bet settled or no qual / no bet opportunity left
        s = S.get('games', {}).get(w['key'], {})
        if s.get('settled'):
            continue
        if start < now - 12 * 3600 and (s.get('bet') or w.get('bet')):
            # bet still open — keep until settled via state; if bet open keep on watch lightly
            if s.get('bet') and not s.get('settled'):
                kept.append(w)
            continue
        if start < now - 12 * 3600 and not s.get('bet') and not w.get('bet'):
            continue
        # drop non-qualifying after official record
        if w.get('recorded') and w.get('qualifies') is False and start > now:
            # still upcoming but won't trade — drop to save fetches
            continue
        kept.append(w)
    watch_doc['games'] = kept
    watch_doc['count'] = len(kept)
    by_league = {}
    for w in kept:
        by_league[w['league']] = by_league.get(w['league'], 0) + 1
    watch_doc['by_league'] = by_league
    watch_doc['updated_et'] = L.now_et()
    watch_doc['updated_at'] = now
    L.save_json(L.WATCH_PATH, watch_doc)

    L.prune_state(S, now)
    S['last_run_et'] = L.now_et()
    S['last_lean_check_et'] = L.now_et()
    L.save_json(L.STATE_PATH, S)

    status = dict(
        event='lean_check',
        ok=True,
        paper=True,
        run_et=L.now_et(),
        watch=len(kept),
        recorded_pregame=recorded_n,
        live_checked=live_checked,
        new_bets=new_bets,
        settlements=sum(1 for e in events if e.get('event') == 'settlement'),
        open_bets=sum(1 for s in S['games'].values() if s.get('bet') and not s.get('settled')),
        errors=errors[:5],
    )
    print(json.dumps(status))
    L.log_line(LOG, f"done {json.dumps({k: status[k] for k in ('watch','recorded_pregame','live_checked','new_bets','settlements','open_bets')})}")
    lock.release()
    return 0


if __name__ == '__main__':
    sys.exit(main())
