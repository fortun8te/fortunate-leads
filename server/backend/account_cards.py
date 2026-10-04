"""HTTP side of the Instagram account cards (see account_view.py and docs/ACCOUNTS_UX.md).

GET  /api/accounts/live[?setup=1]          every card, ETag revision so an unchanged page costs a 304
POST /api/accounts/<lane>/connect          open this account's saved Chrome profile (one bounded attempt)
POST /api/accounts/<lane>/disconnect       stop using it; survives the extension's next check-in
POST /api/accounts/<lane>/reconnect        use it again
POST /api/accounts/<lane>/forget           delete a disconnected account whose Chrome profile is closed

Pause, resume, rename, budgets and reads keep using POST /api/accounts/<lane>. The warning review keeps using
POST /api/control {action: acknowledge_scraping_warning}. Nothing here changes pacing or safety rules.
"""
import json
from datetime import datetime, timezone

import accounts
import account_view
import browser_startup
import map_view

from .common import Bad, EXT_ORIGIN, NotFound


class AccountCardService:

    def __init__(self, config):
        self.config = config

    def _setup(self):
        try:
            manifest = json.loads((self.config.root / 'extension' / 'manifest.json').read_text())
        except (OSError, ValueError):
            manifest = {}
        return {'extension_path': str(self.config.root / 'extension'), 'extension_id': EXT_ORIGIN.split('//')[1],
                'extension_version': manifest.get('version'), 'server': f"http://127.0.0.1:{self.config['port']}"}

    def _payload(self, conn, setup=False):
        return account_view.build(conn, self.config.root, self.config.host_operations_allowed,
                                  setup=self._setup() if setup else None)

    def api_live(self, conn, q, b):
        payload = self._payload(conn, setup=(q.get('setup') or ['0'])[0] == '1')
        return map_view.Raw(json.dumps(payload).encode(), etag='"' + payload['revision'] + '"',
                            headers={'Content-Type': 'application/json'})

    def _write(self, conn, lane, change):
        conn.execute('BEGIN IMMEDIATE')
        try:
            change(conn, lane)
        except LookupError:
            conn.rollback()
            raise NotFound('That account no longer exists.') from None
        except ValueError as exc:
            conn.rollback()
            raise Bad(str(exc)) from None
        conn.commit()
        return self._payload(conn)

    def api_disconnect(self, conn, q, b, lane):
        def change(c, lane):
            accounts.disconnect(c, lane)
            intent = browser_startup.snapshot(c)
            if intent and intent['state'] in browser_startup._PENDING and lane in [a['lane_id'] for a in intent.get('accounts') or []]:
                browser_startup.cancel(c)
        return self._write(conn, lane, change)

    def api_reconnect(self, conn, q, b, lane):
        return self._write(conn, lane, accounts.reconnect)

    def api_forget(self, conn, q, b, lane):
        return self._write(conn, lane, lambda c, l: accounts.forget(c, l))

    def api_connect(self, conn, q, b, lane):
        row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
        if not row:
            raise NotFound('That account no longer exists.')
        if not self.config.host_operations_allowed:
            raise Bad('Accounts cannot connect from this saved-data workspace.')
        if row['disconnected_at']:
            raise Bad('Reconnect this account first.')
        if not row['ig_id']:
            raise Bad('This account has not signed in yet. Open its Chrome profile and sign in to Instagram.')
        try:
            browser_startup.begin(self.config.root, conn, {'action': 'connect_accounts',
                                                           'accounts': [{'lane_id': lane, 'ig_id': row['ig_id']}]})
        except ValueError as exc:
            message = str(exc)
            if 'no saved Chrome profile' in message:
                message = 'No saved Chrome profile for this account. Open its Chrome profile yourself.'
            raise Bad(message) from None
        return self._payload(conn)
