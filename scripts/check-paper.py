"""Read-only local service diagnostic; never prints the dashboard token."""
import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, default=8000)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
token = (root / 'data' / 'dashboard.token').read_text(encoding='ascii').strip()
base = f'http://127.0.0.1:{args.port}'

def get(path, authenticated=True):
    req = urllib.request.Request(base + path, headers={'Authorization': 'Bearer ' + token} if authenticated else {})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)

try:
    get('/api/status', False)
    raise SystemExit('ERROR: private API accepted an anonymous request')
except urllib.error.HTTPError as exc:
    if exc.code != 401:
        raise
state = get('/api/status')
pairs = get('/api/pairs')['pairs']
print(json.dumps({'mode': state['mode'], 'ready': state['ready'], 'paused': state['is_kill_switch_active'],
    'balance': state['balance'], 'active_positions': state['active_positions_count'],
    'closed_trades': state['closed_trades_count'], 'last_tick': state['last_tick'],
    'last_error': state['last_error'], 'clock_offset_seconds': state.get('market_clock_offset_seconds'),
    'models': {p['pair']: {'p_value': p['p_value'], 'eligible': p['is_cointegrated'], 'fresh': p['fresh']} for p in pairs},
    'anonymous_api': 'blocked'}, indent=2))
if not state['ready']:
    raise SystemExit(2)
