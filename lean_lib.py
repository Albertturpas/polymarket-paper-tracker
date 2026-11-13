#!/usr/bin/env python3
"""Shared helpers for lean Polymarket paper cron (extracted/adapted from paper.py). PAPER ONLY."""
import atexit
import csv
import fcntl
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(DIR)

G = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
ET = ZoneInfo('America/New_York')
LEAGUES = {
    '10470': 'NCAAB',
    '39': 'NCAAB',
    '12756': 'CFB',
    '10310': 'Counter-Strike',
    '11634': 'ITF',
}
SLUG2L = {
    'ncaa-cbb': 'NCAAB',
    'cbb': 'NCAAB',
    'march-madness': 'NCAAB',
    'cfb': 'CFB',
    'college-football': 'CFB',
    'counter-strike': 'Counter-Strike',
    'cs2': 'Counter-Strike',
    'itf': 'ITF',
}
EXCL = re.compile(r'draw|map \d|game \d|set \d|1st|first half|quarter|period', re.I)
CSV = 'paper_trades.csv'
CSV_HDR = [
    'date_et', 'league', 'game', 'favorite', 'pregame_pct',
    'fill_price', 'staked_usd', 'status', 'pnl_usd', 'market_url',
]
STATE_PATH = 'paper_state.json'
WATCH_PATH = 'lean_watch.json'
NEW_BETS_PATH = 'logs/new_bets.jsonl'
STAKE = 5.0
PREGAME_MIN = 0.90
BUY_MAX = 0.70
SPREAD_MAX = 0.05
MAX_WATCH = 15
PREGAME_WINDOW_SEC = 600  # record official pregame in the ~10 min before start (tick every 5 min)


def get(u):
    for _ in range(3):
        try:
            return json.load(urllib.request.urlopen(
                urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'}),
                timeout=30,
            ))
        except Exception:
            time.sleep(2)
    return None


def ts(s):
    s = s.replace(' ', 'T').replace('Z', '+00:00')
    if s.endswith('+00'):
        s += ':00'
    return datetime.fromisoformat(s).timestamp()


def now_et():
    return datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')


def league_of(slug):
    s = (slug or '').lower()
    for k, v in SLUG2L.items():
        if s == k or s.startswith(k + '-') or s.startswith(k + '-games'):
            return v
    return None


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def ensure_csv():
    if not os.path.exists(CSV):
        with open(CSV, 'w', newline='') as f:
            csv.writer(f).writerow(CSV_HDR)


def append_trade(row):
    ensure_csv()
    with open(CSV, 'a', newline='') as f:
        csv.writer(f).writerow([row[h] for h in CSV_HDR])


def rewrite_trades(rows):
    with open(CSV, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=CSV_HDR)
        w.writeheader()
        w.writerows(rows)


def read_trades():
    ensure_csv()
    with open(CSV) as f:
        return list(csv.DictReader(f))


def append_new_bet_event(row):
    os.makedirs('logs', exist_ok=True)
    ev = dict(event='new_trade', paper=True, **row)
    with open(NEW_BETS_PATH, 'a') as f:
        f.write(json.dumps(ev) + '\n')


class Lock:
    """Exclusive lockfile; second concurrent run exits cleanly (returns False)."""

    def __init__(self, path):
        self.path = path
        self.fd = None

    def acquire(self):
        try:
            self.fd = open(self.path, 'w')
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd.write(str(os.getpid()) + '\n')
            self.fd.flush()
            atexit.register(self.release)
            return True
        except (BlockingIOError, OSError):
            if self.fd:
                try:
                    self.fd.close()
                except Exception:
                    pass
                self.fd = None
            return False

    def release(self):
        if not self.fd:
            return
        try:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
            self.fd.close()
        except Exception:
            pass
        self.fd = None
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except Exception:
            pass


def side(g, fav):
    """Return (mid, ask) for favorite side from two-outcome book on outcome index 0."""
    i = g['oc'].index(fav)
    bb, ba = g['bb'], g['ba']
    return ((bb + ba) / 2, ba) if i == 0 else (1 - (bb + ba) / 2, 1 - bb)


def parse_market(m, e, lg):
    """Build a game dict from a moneyline market, or None if filtered out."""
    if not m.get('gameStartTime') or EXCL.search(m.get('question', '')):
        return None
    try:
        oc = json.loads(m['outcomes'])
    except Exception:
        return None
    if len(oc) != 2 or oc[0] == 'Yes':
        return None
    try:
        tokens = json.loads(m.get('clobTokenIds') or '[]')
    except Exception:
        tokens = []
    st = ts(m['gameStartTime'])
    bb, ba = m.get('bestBid'), m.get('bestAsk')
    ok = bool(bb) and ba is not None and ba - bb <= SPREAD_MAX + 1e-9
    return dict(
        mid_id=m['id'],
        tokens=tokens,
        eslug=e.get('slug') or m.get('slug'),
        title=e.get('title') or m.get('question'),
        league=lg,
        start=st,
        oc=oc,
        bb=bb,
        ba=ba,
        ok=ok,
        closed=bool(m.get('closed')),
        outcome_prices=m.get('outcomePrices'),
    )


def scan_candidate_markets(horizon_hours=36, lookback_hours=10):
    """Collect two-outcome moneylines for configured leagues (same as paper.py)."""
    now = time.time()
    mins = datetime.fromtimestamp(now - 2 * 86400, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    cand = {}
    cover = {}
    for sid, lg in LEAGUES.items():
        off = 0
        while off < 1000:
            d = get(f'{G}/events?series_id={sid}&closed=false&end_date_min={mins}&limit=50&offset={off}')
            if not d:
                break
            for e in d:
                for m in e.get('markets') or []:
                    if m.get('sportsMarketType') == 'moneyline' and not m.get('closed'):
                        cand[m['id']] = (m, e, lg)
            off += 50
            if len(d) < 50:
                break
    for p in ('start_date_min', 'end_date_min'):
        off = 0
        while off < 2100:
            d = get(f'{G}/markets?sports_market_types=moneyline&closed=false&limit=100&offset={off}&{p}={mins}')
            if not d:
                break
            for m in d:
                e = (m.get('events') or [{}])[0]
                lg = league_of(e.get('seriesSlug'))
                if lg and m['id'] not in cand:
                    cand[m['id']] = (m, e, lg)
            off += 100
            if len(d) < 100:
                break
    games = {}
    for mid, (m, e, lg) in cand.items():
        g = parse_market(m, e, lg)
        if not g:
            continue
        if not (now - lookback_hours * 3600 < g['start'] < now + horizon_hours * 3600):
            continue
        key = g['eslug'] or mid
        if key in games:
            continue
        games[key] = g
        cover[lg] = cover.get(lg, 0) + 1
    return games, cover, len(cand)


def fetch_market_by_id(market_id):
    d = get(f'{G}/markets?id={market_id}')
    if not d:
        return None
    m = d[0]
    e = (m.get('events') or [{}])[0]
    lg = league_of(e.get('seriesSlug')) or None
    # fall back to watch/state league if slug missing
    g = parse_market(m, e, lg or 'UNK')
    if g:
        g['raw'] = m
    return g


def settle_open(S, now):
    """Settle open paper bets whose markets closed. Mutates S, returns event list."""
    events = []
    for k, s in list(S.get('games', {}).items()):
        if not s.get('bet') or s.get('settled') or s.get('start', 0) > now:
            continue
        d = get(f"{G}/markets?id={s['market_id']}&closed=true") or get(f"{G}/markets?id={s['market_id']}")
        if not d or not d[0].get('closed'):
            continue
        m = d[0]
        try:
            oc = json.loads(m['outcomes'])
            pr = [float(x) for x in json.loads(m['outcomePrices'])]
        except Exception:
            continue
        if max(pr) < 0.99:
            continue
        won = oc[pr.index(max(pr))] == s['fav']
        pnl = round(s['shares'] - STAKE if won else -STAKE, 2)
        s.update(settled=True, won=won, pnl=pnl)
        rows = read_trades()
        for r in rows:
            if r['market_url'] == s['url'] and r['status'] == 'open':
                r['status'] = 'won' if won else 'lost'
                r['pnl_usd'] = pnl
        rewrite_trades(rows)
        events.append(dict(
            event='settlement', paper=True, game=s['title'], league=s['league'],
            favorite=s['fav'], status='won' if won else 'lost', pnl_usd=pnl,
            market_url=s['url'],
        ))
    return events


def prune_state(S, now, max_age_days=3):
    for k in list(S.get('games', {})):
        s = S['games'][k]
        if s.get('start', 0) < now - max_age_days * 86400 and (not s.get('bet') or s.get('settled')):
            del S['games'][k]


def log_line(path, msg):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a') as f:
        f.write(f'{now_et()} {msg}\n')
