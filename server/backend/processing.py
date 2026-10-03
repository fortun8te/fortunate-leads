"""ProcessingService owns one workspace and its request dependencies."""

import biofetch
import deepscout
import sqlite3
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import processing_progress
import db
import owner_notes
import processing_modes
import processing_state
import local_model
import k2_connection
import resource_budget
import engine_controls
import laya
import llm
import usage_ledger
import threading
from pathlib import Path
from .common import Bad, NotFound
from .queries import counts


class ProcessingService:

    def __init__(self, config, local_services, qualification):
        self.config = config
        self.local_services = local_services
        self.qualification = qualification
        self.summary_cache = {}
        self.summary_lock = threading.Lock()
        self.host_operations_allowed = config.host_operations_allowed

    def require_settings_write(self, path):
        # Tests and isolated workspaces can provide files inside their own data
        # directory. A preview cannot alter the Mac's shared service settings.
        if self.config.saved_data_only or (not self.host_operations_allowed and not Path(path).resolve().is_relative_to(
                Path(self.config.db).resolve().parent)):
            raise Bad('Service settings cannot change from this saved-data workspace.')

    def api_biofetch_get(self, conn, q, b=None):
        return biofetch.public(conn)

    def api_biofetch(self, conn, q, b):
        """{"on"?, "token"?, "ig_user_id"?, "gap"?}: bios from the Meta Graph API (business_discovery), off by default."""
        try:
            return biofetch.save(conn, b)
        except ValueError as e:
            raise Bad(str(e))

    def api_qualify(self, conn, q, b):
        """{"on"?, "auto"?, "local_laya"?, "workers"?, "llm_min"?, "bio_min"?}: absent keys stay unchanged."""
        if 'on' in b and not isinstance(b['on'], bool):
            raise Bad('on must be true or false')
        if 'auto' in b and not isinstance(b['auto'], bool):
            raise Bad('auto must be true or false')
        if 'local_laya' in b and not isinstance(b['local_laya'], bool):
            raise Bad('local_laya must be true or false')
        if b.get('auto'):
            raise Bad('Choose RLEAI to enable external AI. It never starts automatically.')
        for key, lo, hi in (('workers', 1, 32), ('llm_min', 0, 100), ('bio_min', 0, 100)):
            if key in b and (not isinstance(b[key], int) or isinstance(b[key], bool) or not lo <= b[key] <= hi):
                raise Bad(f'{key} must be a whole number {lo}-{hi}')
        if 'on' in b or 'local_laya' in b:
            external = b.get('on', processing_modes.allows(conn, 'external'))
            local = b.get('local_laya', processing_modes.allows(conn, 'laya'))
            processing_modes.set_mode(conn, 'RLEAI' if external else 'RLAI' if local else 'R')
        if 'auto' in b:
            # An explicit request to schedule later activation is separate from switching off now.
            db.set_setting(conn, 'qualify_auto', b['auto'])
        if 'workers' in b:
            db.set_setting(conn, 'llm_workers', b['workers'])
        for key in ('llm_min', 'bio_min'):
            if key in b:
                db.set_setting(conn, key, b[key])
        conn.commit()
        result = {'qualify': bool(db.get_setting(conn, 'qualify')), 'processing': processing_modes.snapshot(conn)}
        if 'on' in b or 'local_laya' in b:
            self.local_services.schedule(conn)
        if 'local_laya' in b:
            result['local_laya'] = bool(db.get_setting(conn, 'local_laya'))
        return result

    def api_llm(self, conn, q, b):
        """Providers (keys masked), cooldowns, requests today, last error; plus the Laya sidecar and the pool settings."""
        out = llm.get().status()
        out.update(workers=db.get_setting(conn, 'llm_workers'), llm_min=db.get_setting(conn, 'llm_min'),
                   bio_min=db.get_setting(conn, 'bio_min'), laya={'url': laya.URL, 'up': laya.last_known()},
                   config=str(llm.CONFIG.relative_to(self.config.root)) if llm.CONFIG.is_relative_to(self.config.root) else str(llm.CONFIG))
        auto = llm.read_config().get('auto_models') or {}
        out['auto_models'] = {k: auto.get(k) for k in ('stealth', 'free', 'at', 'error', 'new_stealth')}
        counts = {}
        for p in out['providers']:
            counts[p.get('state') or 'untested'] = counts.get(p.get('state') or 'untested', 0) + 1
        out['summary'] = counts   # e.g. {'ok': 2, 'spent': 2, 'error': 1}: spent keys are not broken, they return at 00:00 UTC
        out['verdicts'] = dict(conn.execute("SELECT CASE WHEN model IN ('rules','error') THEN model ELSE 'llm' END, count(*) FROM verdicts "
                                            'GROUP BY 1').fetchall())
        out['usage'] = self.llm_usage_report()
        return out

    def llm_usage_report(self, days=30, purpose='all'):
        pool = llm.get()
        path = pool.usage_path or usage_ledger.PATH
        gap = pool.usage_gap or usage_ledger.read_gap(path)
        try:
            report = usage_ledger.summary(path, days, purpose)
        except (OSError, ValueError, sqlite3.Error):
            return {'available': False, 'error': 'usage_ledger_unavailable',
                    'accounting_gap': gap, 'window_days': days, 'purpose': purpose}
        first = report['recording_since']
        covered_window = bool(first) and datetime.fromisoformat(first) <= datetime.now(timezone.utc) - timedelta(days=days)
        return dict(report, available=True, complete=gap is None and covered_window, accounting_gap=gap)

    def api_llm_usage(self, conn, q, b):
        raw = (q.get('days') or ['30'])[0]
        if not raw.isdigit() or not 1 <= int(raw) <= 365:
            raise Bad('days must be 1-365')
        purpose = (q.get('purpose') or ['all'])[0]
        if purpose not in ('qualification', 'website_summary', 'provider_test', 'external_broad', 'external_deep', 'all'):
            raise Bad('unknown usage purpose')
        return self.llm_usage_report(int(raw), purpose)

    def api_llm_health(self, conn, q, b):
        """Probes now: is the local OpenRouter proxy listening, does the Laya sidecar answer /health."""
        url = urlparse(llm.get().proxy)
        try:
            socket.create_connection((url.hostname, url.port or 80), timeout=1).close()
            proxy = True
        except OSError:
            proxy = False
        laya.reset()
        return {'proxy': {'url': f'{url.scheme}://{url.netloc}', 'up': proxy}, 'laya': {'url': laya.URL, 'up': laya.available()}}

    def api_llm_key_add(self, conn, q, b):
        self.require_settings_write(llm.CONFIG)
        try:
            return {'id': llm.add_key(b.get('key'))}
        except ValueError as e:
            raise Bad(str(e)) from None

    def api_llm_key_remove(self, conn, q, b, pid):
        self.require_settings_write(llm.CONFIG)
        try:
            llm.remove_key(pid)
        except LookupError:
            raise NotFound('no such key') from None
        except ValueError as e:
            raise Bad(str(e)) from None
        return {}

    def api_llm_key_test(self, conn, q, b, pid):
        self.require_settings_write(llm.CONFIG)
        try:
            return llm.get().test(pid)
        except LookupError:
            raise NotFound('no such key') from None

    def api_scout(self, conn, q, b=None):
        return deepscout.status(conn)

    def api_scout_set(self, conn, q, b):
        if 'on' in b:
            if not isinstance(b['on'], bool):
                raise Bad('on must be true or false')
            if b['on'] and not processing_modes.allows(conn, 'external'):
                raise Bad('Deep research requires RLEAI mode.')
            db.set_setting(conn, 'scout', b['on'])
        if 'model' in b:
            if b['model'] not in deepscout.MODELS:
                raise Bad('unknown model')
            db.set_setting(conn, 'scout_model', b['model'])
        if 'workers' in b:
            if isinstance(b['workers'], bool) or not isinstance(b['workers'], int) or not 1 <= b['workers'] <= 8:
                raise Bad('workers must be 1-8')
            db.set_setting(conn, 'scout_workers', b['workers'])
        conn.commit()
        return deepscout.status(conn)

    def api_llm_models_refresh(self, conn, q, b):
        self.require_settings_write(llm.CONFIG)
        rec = llm.refresh_models(force=True)
        st = llm.get().status()
        return {'models': st['models'], 'auto': {k: rec.get(k) for k in ('stealth', 'free', 'at', 'error', 'new_stealth')}}

    def api_llm_models(self, conn, q, b):
        self.require_settings_write(llm.CONFIG)
        try:
            llm.set_models(b.get('models'), b.get('daily_limit'))
        except ValueError as e:
            raise Bad(str(e)) from None
        st = llm.get().status()
        return {'models': st['models'], 'daily_limit': st['daily_limit']}

    def api_processing_mode(self, conn, q, b):
        if 'mode' in b:
            try:
                with engine_controls.lock:
                    processing_modes.set_mode(conn, b['mode'])
                    conn.commit()
            except ValueError as exc:
                raise Bad(str(exc)) from None
            conn.commit()
            self.local_services.schedule(conn)
        return dict(processing_modes.snapshot(conn), services=dict(self.local_services.state))

    def notes_pending(self, conn):
        return sum(owner_notes.result(conn, r[0])['state'] in ('pending', 'unavailable')
                   for r in conn.execute("SELECT person_id FROM marks WHERE trim(coalesce(note,''))<>''"))

    def api_note_retry(self, conn, q, b, pid):
        if not conn.execute('SELECT 1 FROM people WHERE id=?', (pid,)).fetchone():
            raise NotFound('No such profile')
        if not processing_modes.allows(conn, 'notes'):
            raise Bad('Choose RLAI to read notes locally.')
        queued = owner_notes.retry(conn, int(pid))
        conn.commit()
        if queued:
            self.local_services.schedule(conn)
        return {'queued': queued}

    def local_processing_counts(self, conn):
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        key = path or ('memory', id(conn))
        now = time.monotonic()
        with self.summary_lock:
            hit = self.summary_cache.get(key) if path else None
            if hit and now - hit[0] < 15 and not conn.in_transaction:
                return hit[1]
        out = dict(processing_state.queue_status(conn), counts=dict(conn.execute(
            'SELECT status,count(*) FROM local_reviews GROUP BY status')))
        if path and not conn.in_transaction:
            with self.summary_lock:
                if len(self.summary_cache) > 32:
                    self.summary_cache.clear()
                self.summary_cache[key] = (now, out)
        return out

    def engine_snapshot(self, conn, runtime=None):
        processing = processing_modes.snapshot(conn)
        runtime = local_model.status() if runtime is None else runtime
        if laya.last_known() is None:
            laya.available()  # Observe a loaded sidecar even when this app starts in Rules.
        out = engine_controls.snapshot(processing, {
            'k2': dict(runtime, managed_local=not local_model.is_remote()),
            'laya': dict(laya.runtime_status(), resources=resource_budget.state()),
        }, starting=self.local_services.lock.locked())
        out['external_active'] = self.qualification.external_active()
        out['stop_acknowledged'] = out['stop_acknowledged'] and not out['external_active']
        return out

    def api_engines(self, conn, q, b):
        if b:
            try:
                with engine_controls.lock:
                    processing_modes.set_engine(conn, b.get('engine'), b.get('enabled'))
                    conn.commit()
            except ValueError as exc:
                raise Bad(str(exc)) from None
            self.local_services.schedule(conn)
        return self.engine_snapshot(conn)

    def api_k2_connection(self, conn, q, b):
        if b:
            self.require_settings_write(k2_connection.CONFIG_PATH)
            try:
                with engine_controls.lock:
                    enabled = (processing_modes.snapshot(conn)['engines']['k2']['enabled']
                               if conn is not None else False)
                    if (enabled or engine_controls.has_active('k2') or local_model._lock.locked()
                            or local_model._activity_unknown or self.local_services.lock.locked()
                            or (local_model.SERVICE_ROOT / 'pid').exists()):
                        raise Bad('Turn K2 off and wait for it to stop before changing its connection')
                    saved = k2_connection.save(b)
                    local_model._remote_ready_endpoint = None
                    return dict(saved, has_api_key=bool(k2_connection.read_secret()['api_key']))
            except ValueError as exc:
                raise Bad(str(exc)) from None
        try:
            saved = k2_connection.read()
            return dict(saved, has_api_key=bool(k2_connection.read_secret()['api_key']))
        except ValueError as exc:
            raise Bad(str(exc)) from None

    def api_k2_connection_test(self, conn, q, b):
        self.require_settings_write(k2_connection.CONFIG_PATH)
        if b:
            raise Bad('Save the connection before testing it')
        return local_model.test_connection()

    def api_local_processing(self, conn, q, b):
        if 'paused' in b:
            try:
                with engine_controls.lock:
                    processing_modes.set_paused(conn, b['paused'])
                    conn.commit()
            except ValueError as exc:
                raise Bad(str(exc)) from None
            conn.commit()
            self.local_services.schedule(conn)
        enabled = (processing_modes.allows(conn, 'local_qualification')
                   and processing_modes.snapshot(conn)['engines']['k2']['enabled'])
        paused = processing_modes.snapshot(conn).get('paused', False)
        summary = dict(self.local_processing_counts(conn))
        counts = summary.pop('counts')
        runtime = local_model.status()
        engines = self.engine_snapshot(conn, runtime)
        pending = self.notes_pending(conn) if enabled else 0
        failed = sum(owner_notes.result(conn, r[0])['state'] == 'failed' for r in conn.execute(
            "SELECT person_id FROM marks WHERE trim(coalesce(note,''))<>''")) if enabled else 0
        budget = runtime.get('resources', {})
        waiting = budget.get('recovering') or budget.get('thermal_limited') or budget.get('error')
        state = ('stopping' if (paused and engines['external_active']) or any(
                     e['state'] == 'stopping' for e in engines['engines'].values()) else
                 'off' if not enabled else 'paused' if paused else 'waiting_for_mac' if waiting else
                 'starting' if self.local_services.state['state'] == 'starting' else 'unavailable' if not runtime.get('ready') else
                 'working' if engines['engines']['k2']['active'] and not engines['engines']['k2'].get('activity_unknown') else
                 'waiting' if summary.get('pending') or pending or summary.get('seeding') else 'ready')
        return dict(summary, queue=summary['pending'],
                    reviewed=counts.get('complete', 0) + counts.get('needs_research', 0),
                    unverified=counts.get('unverified', 0) + counts.get('insufficient_evidence', 0),
                    needs_research=counts.get('needs_research', 0), enabled=enabled,
                    model=local_model.MODEL, ready=bool(runtime.get('ready')),
                    notes_pending=pending, notes_failed=failed, state=state, runtime=runtime, paused=paused,
                    processing=processing_modes.snapshot(conn), engines=engines['engines'],
                    stop_acknowledged=engines['stop_acknowledged'],
                    progress=processing_progress.snapshot(conn, summary['pending'], paused=paused or not enabled,
                                                           seeding=summary.get('seeding'), waiting=bool(waiting)),
                    history=processing_state.recent_history(conn, 6))
