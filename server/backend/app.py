"""Composition of saved-data services, HTTP transport and owned background work."""

import threading
import time
from pathlib import Path

import biofetch
import browser_startup
import control
import db
import deepscout
import dm_import
import map_layout
import map_universe
import map_view
import note_mentions
import owner_notes
import processing_modes
import processing_state
import qual_api
import workflows

from backend_cache import CacheBusy, CacheStore
from backend_http import HttpApplication, Handler, Server
from storage.connection import ConnectionFactory

from .common import Bad, Conflict, EXT_ORIGIN, NotFound
from .collection import CollectionService
from .leads import LeadService
from .maps import MapService
from .photos import PhotoService
from .processing import ProcessingService
from .profile_planner import plan_profiles
from .qualification import QualificationService
from .runtime import LocalServices, WorkerSupervisor

LANE = r'(?P<lane>[A-Za-z0-9_-]{1,64})'
KEY = r'(?P<key>proxy|[0-9a-f]{10})'


class Application:
    """One workspace. Constructing it neither opens data nor starts workers."""

    def __init__(self, config):
        self.config = config
        self.cache = CacheStore()
        self.local_services = LocalServices(config)
        self.qualification = QualificationService(config)
        self.photos = PhotoService(config)
        self.leads = LeadService(config, self.cache)
        self.maps = MapService(config, self.cache)
        self.processing = ProcessingService(config, self.local_services, self.qualification)
        self.collection = CollectionService(config, self.cache, self.qualification,
                                            self.processing, self.local_services)
        # Request connections retain at most 2 MiB of page cache each. Separate
        # background connections retain the established writer wait and 32 MiB.
        self.request_connections = ConnectionFactory(config.db, timeout=0.25, cache_kib=2048)
        self.worker_connections = ConnectionFactory(config.db)
        self.supervisor = WorkerSupervisor(self.worker_connections)
        self.scouts = deepscout.ScoutPool(config.db)
        self.collection.background_probe = self.background_busy
        self.routes = self._routes()
        self.http = self.http_application()
        self.server = None
        self.started = False
        self.closed = False
        self.lifecycle_lock = threading.RLock()
        self.serving = threading.Event()
        self.serve_thread = None

    def _routes(self):
        routes = [('GET', '/api/mobile/state', self.collection.api_mobile_state),
            ('POST', '/api/mobile/queue', self.collection.api_mobile_queue),
            ('GET', '/api/processing-mode', self.processing.api_processing_mode),
            ('POST', '/api/processing-mode', self.processing.api_processing_mode),
            ('GET', '/api/engines', self.processing.api_engines),
            ('POST', '/api/engines', self.processing.api_engines),
            ('GET', '/api/k2/connection', self.processing.api_k2_connection),
            ('POST', '/api/k2/connection', self.processing.api_k2_connection),
            ('POST', '/api/k2/connection/test', self.processing.api_k2_connection_test),
            ('GET', '/api/local-processing', self.processing.api_local_processing),
            ('POST', '/api/local-processing', self.processing.api_local_processing),
            ('POST', '/api/person/(\\d+)/note-retry', self.processing.api_note_retry),
            ('GET', '/api/accounts', self.collection.api_accounts),
            ('POST', f'/api/accounts/{LANE}', self.collection.api_account_edit),
            ('POST', f'/api/accounts/{LANE}/remove', self.collection.api_account_remove),
            ('GET', '/api/setup', self.collection.api_setup),
            ('GET', '/api/onboarding', self.collection.api_onboarding),
            ('POST', '/api/onboarding', self.collection.api_onboarding),
            ('POST', '/api/start', self.collection.api_start),
            ('POST', '/api/settings/accounts', self.collection.api_account_settings),
            ('GET', '/api/benchmark/next', self.collection.benchmark_next),
            ('POST', '/api/benchmark/permit', self.collection.benchmark_permit),
            ('POST', '/api/benchmark/result', self.collection.benchmark_result),
            ('GET', '/api/ext/next', self.collection.ext_next),
            ('POST', '/api/ext/list-page', self.collection.ext_list_page),
            ('POST', '/api/ext/profile', self.collection.ext_profile),
            ('POST', '/api/ext/error', self.collection.ext_error),
            ('POST', '/api/ext/request', self.collection.ext_request),
            ('POST', '/api/ext/heartbeat', self.collection.ext_heartbeat),
            ('GET', '/api/control', self.collection.api_control),
            ('POST', '/api/control', self.collection.api_control_set),
            ('POST', '/api/engine/start', self.collection.api_engine_start),
            ('GET', '/api/ext/control', self.collection.api_control),
            ('POST', '/api/ext/control', self.collection.api_control_set),
            ('GET', '/api/leads', self.leads.api_leads),
            ('GET', '/api/tags', self.leads.api_tags),
            ('GET', '/api/counts', self.leads.api_counts),
            ('POST', '/api/tags/rename', self.leads.api_tag_rename),
            ('POST', '/api/tags/delete', self.leads.api_tag_delete),
            ('GET', '/api/tag-rules', self.leads.api_rules),
            ('POST', '/api/tag-rules', self.leads.api_rule_add),
            ('GET', '/api/tag-rules/preview', self.leads.api_rule_preview),
            ('POST', '/api/tag-rules/(\\d+)/delete', self.leads.api_rule_delete),
            ('GET', '/api/person/(\\d+)', self.leads.api_person),
            ('POST', '/api/person/(\\d+)/mark', self.leads.api_mark),
            ('POST', '/api/person/(\\d+)/tags', self.leads.api_tag_edit),
            ('POST', '/api/person/(\\d+)/read', self.leads.api_read),
            ('GET', '/api/map/view', self.maps.api_map_view),
            ('GET', '/api/map/search', self.maps.api_map_search),
            ('GET', '/api/map/edges', self.maps.api_map_edges),
            ('GET', '/api/map', self.maps.api_map),
            ('GET', '/api/connections', self.maps.api_connections),
            ('GET', '/api/map-overview', self.maps.api_map_overview),
            ('GET', '/api/note-mentions', lambda conn, q, b: note_mentions.search(conn, q.get('q', [''])[0])),
            ('GET', '/api/scraper/status', self.collection.api_scraper_status),
            ('GET', '/api/scraper', self.collection.api_scraper),
            ('GET', '/api/scraper/suggestions', self.collection.api_collection_suggestions),
            ('POST', '/api/scraper/suggestions', self.collection.api_collection_suggestions),
            ('POST', '/api/scraper/seeds', self.collection.api_seeds),
            ('POST', '/api/scraper/pause', self.collection.api_pause),
            ('POST', '/api/scraper/budget', self.collection.api_budget),
            ('POST', '/api/scraper/snowball', self.collection.api_snowball),
            ('POST', '/api/settings/qualify', self.processing.api_qualify),
            ('GET', '/api/settings/biofetch', self.processing.api_biofetch_get),
            ('POST', '/api/settings/biofetch', self.processing.api_biofetch),
            ('GET', '/api/llm', self.processing.api_llm),
            ('GET', '/api/llm/usage', self.processing.api_llm_usage),
            ('GET', '/api/llm/health', self.processing.api_llm_health),
            ('POST', '/api/llm/keys', self.processing.api_llm_key_add),
            ('POST', f'/api/llm/keys/{KEY}/remove', self.processing.api_llm_key_remove),
            ('POST', f'/api/llm/keys/{KEY}/test', self.processing.api_llm_key_test),
            ('GET', '/api/scout', self.processing.api_scout),
            ('POST', '/api/settings/scout', self.processing.api_scout_set),
            ('POST', '/api/llm/models', self.processing.api_llm_models),
            ('POST', '/api/llm/models/refresh', self.processing.api_llm_models_refresh)]
        routes += map_universe.routes(self.maps._map_database)
        routes += qual_api.routes(self.leads)
        routes += workflows.routes()
        routes += dm_import.routes()
        return routes

    def http_application(self, routes=None):
        return HttpApplication(
            config=self.config, connect=lambda path: self.request_connections(),
            routes=self.routes if routes is None else routes,
            bad=Bad, not_found=NotFound, conflict=Conflict, raw_type=map_view.Raw,
            web_root=self.config.web, pictures_dir=self.photos.pfp_dir,
            valid_picture=self.photos.valid_pic, extension_origin=EXT_ORIGIN,
            retryable_errors=(CacheBusy,),
        )

    def bind_port(self, port):
        """Publish the actual listener port after an ephemeral bind."""
        self.config = self.config.with_port(port)
        for service in (self.local_services, self.qualification, self.photos, self.leads,
                        self.maps, self.processing, self.collection):
            service.config = self.config
        self.http.config = self.config

    def initialize(self):
        """Prepare this explicitly selected database; never clear operator holds."""
        Path(self.config.db).parent.mkdir(parents=True, exist_ok=True)
        conn = db.init(self.config.db)
        try:
            deepscout.ensure(conn)
            owner_notes.ensure(conn)
            processing_state.ensure(conn)
            processing_modes.set_mode(conn, processing_modes.current_mode(conn))
            conn.commit()
            self.qualification.retag_if_changed(conn)
            self.qualification.refresh_laya_prefilter_if_changed(conn)
            if self.config.host_operations_allowed:
                browser_startup.schedule(self.config.root, conn)
        finally:
            conn.close()

    def background_busy(self):
        busy = ['background_worker'] if self.supervisor.busy() else []
        if self.qualification.external_active():
            busy.append('external_ai')
        with self.scouts.lock:
            if self.scouts.inflight:
                busy.append('research')
        return busy

    def repair_step(self, conn):
        out = db.repair_lists(conn)
        conn.commit()
        if any(out.values()):
            print('repair_lists', out, flush=True)
        return False

    def local_services_step(self, conn):
        return self.local_services.step(conn, self.processing.notes_pending)

    def background_qualify(self, conn):
        refreshed = self.qualification.drain_network_dirty(conn)
        if not db.get_setting(conn, 'local_laya') and all(
                control.stage_paused(conn, stage) for stage in ('lists', 'bios', 'ai')):
            return bool(refreshed)
        return self.qualification.qualify_batch(conn) or bool(refreshed)

    def map_layout_step(self, conn):
        out = map_layout.step(self.maps._map_database(conn), conn)
        return bool(out.get('apply', {}).get('applied'))

    def start_workers(self, stop=None):
        with self.lifecycle_lock:
            if self.closed:
                raise RuntimeError('Application is closed')
            if self.config.saved_data_only:
                raise RuntimeError('Background work is disabled in this saved-data workspace')
            if self.started:
                return self.supervisor
            if stop is not None:
                self.supervisor.stop = stop
            pool = self.qualification.llm_pool
            loops = [
                ('repair', self.repair_step, 900, 900),
                ('local-services', self.local_services_step, 30, 30),
                ('rules', self.background_qualify, 0.2, 5),
                ('processing-maintenance', self.qualification.processing_maintenance, 0.1, 5),
                ('external-ai', pool.step, 1, 5),
                ('laya', self.qualification.laya_step, 0.2, 30),
                ('local-processing', self.qualification.local_processing_step, 0.1, 5),
                ('profile-planner', plan_profiles, 15, 15),
                ('photos', self.photos.pfp_step, 0.4, 10),
                ('biofetch', biofetch.step, 0.5, 10),
                ('research', self.scouts.step, 2, 10),
                ('map-layout', self.map_layout_step, 1, 10),
            ]
            for loop in loops:
                self.supervisor.start(*loop)
            self.started = True
            return self.supervisor

    def serve(self, workers=True):
        try:
            self.initialize()
            with self.lifecycle_lock:
                if self.closed:
                    raise RuntimeError('Application is closed')
                self.server = Server(('127.0.0.1', self.config.port), Handler, application=self.http)
                self.bind_port(self.server.server_address[1])
                self.serve_thread = threading.current_thread()
                self.serving.set()
            if workers:
                self.start_workers()
            print(f'Fortunate Leads on http://127.0.0.1:{self.server.server_address[1]}  '
                  f'db={self.config.db}', flush=True)
            self.server.serve_forever()
        finally:
            self.serving.clear()
            self.close()

    def close(self, timeout=5):
        """Stop admission, join bounded loops and report external work still finishing."""
        with self.lifecycle_lock:
            self.closed = True
            server = self.server
        deadline = time.monotonic() + timeout
        if server is not None:
            if threading.current_thread() is not self.serve_thread:
                server.shutdown()
            server.server_close()
        pending = self.supervisor.close(max(0, deadline - time.monotonic()))
        if not self.local_services.close(max(0, deadline - time.monotonic())):
            pending.append('local-service-start')
        if self.qualification.external_active():
            pending.append('external_ai')
        with self.scouts.lock:
            if self.scouts.inflight:
                pending.append('research')
        # Public-site/model calls already have finite deadlines. They finish on
        # their owning connections; shutdown does not wait indefinitely here.
        self.qualification.shutdown(wait=False)
        self.scouts.shutdown(wait=False)
        self.cache.clear()
        return pending
