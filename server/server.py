"""CLI and ordinary compatibility exports for the Fortunate Leads backend.

Production request and worker code lives in backend/. Existing local tools can
still import the established entry point; new code imports its actual owner.
"""
import argparse
import os
import signal
from datetime import datetime
import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import biofetch
import deepscout
import websearch
import accounts
import edge_benchmark_api
import mobile_collector
import control
import connection_graph
import collection_suggestions
import collection_progress
import map_scale
import map_layout
import map_view
import map_universe
import note_mentions
import dm_import
import processing_progress
import db
import owner
import owner_relationships
import tag_projection
import owner_notes
import engine_start
import browser_startup
import onboarding
import processing_modes
import processing_state
import local_model
import k2_connection
import resource_budget
import engine_controls
import local_qualification
import external_harness
import external_queue
import meta_network
import laya
import llm
import usage_ledger
import qualify
import rules
import workflows
import qual_api

from backend.app import Application
from backend.common import (
    AppConfig,
    status_in,
    Bad,
    NotFound,
    Conflict,
    text_or_none,
    utc,
    iso,
    workspace_cooldown,
    count_or_none,
    metric_int,
    clean_iso,
    clean_rate,
    judge,
    chunks,
    csv,
    qint,
    read_snapshot,
    data_rev,
    WorkerDelay,
    EXT_ORIGIN,
    STATUSES,
    POSITIVE,
    POSITIVE_SQL,
    LEGACY_STATUS,
    PIC_HOSTS,
    PIC_MAX,
    READ_PRIORITY,
    LEASE_MIN,
    PROFILE_MAX_ATTEMPTS,
    QUALIFY_MAX_ATTEMPTS,
    PLAN_BATCH,
    OWNER_HANDLE,
    FOLLOWING_TRIAL_PAGE_LIMIT,
    LISTS_READ_THROUGH,
    LISTS,
    PEOPLE_FROM,
    LEAD_SQL,
    NOT_ME,
    TAG_ORDER,
    MAP_TAGS,
    JUDGE_BAD,
    JUDGE_GOOD,
    SORTS,
    KEEP,
    SEED_LINKS_TOP,
    RATE_WINDOW,
    OBSERVED_RATE_WINDOW,
    SNOWBALL_MAX,
)
from backend.evidence import (
    me_handle,
    edges_of,
    edge_history_of,
    network_context,
    network_snapshot,
    with_owner,
)
from backend.queries import (
    lead_rows,
    lead_filter,
    tag_facets,
    counts,
    person_row,
)
from backend.owner_edits import (
    set_status,
    clean_tag,
    manual_tag,
    tag_group,
    touch,
    add_manual,
    rule_out,
)
from backend.profile_planner import (
    auto_qualify,
    plan_priority,
    plan_profiles,
    EARLY_LISTS,
)
from backend.qualification import (
    FEWSHOT_MAX, FEWSHOT_CHANGE, FEWSHOT_RERUN, FEWSHOT_TAG_MAX,
    LAYA_BATCH, LAYA_REBUILD_BATCH, laya_hash, external_candidates_sql,
    LLMPool as QualificationPool, _expire,
)
from backend.photos import NoRedirect
from backend_http import Server as HttpServer, bind_handler

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'web'
CFG = {'db': str(ROOT / 'data' / 'leads.sqlite'), 'port': 8777}
_application = None
_application_lock = threading.RLock()
ROUTES = []
CACHE = None
CACHE_LOCK = None
SEED_LINKS = None
CACHE_MAX = 32


def get_application():
    """Compatibility callers select a workspace through the established CFG."""
    global _application, ROUTES, CACHE, CACHE_LOCK, SEED_LINKS
    config = AppConfig.from_mapping(CFG, ROOT)
    with _application_lock:
        if _application is None or _application.config != config or _application.closed:
            _application = Application(config)
            ROUTES = _application.routes
            CACHE = _application.cache.values
            CACHE_LOCK = _application.cache.lock
            SEED_LINKS = _application.cache.seed_links
        return _application


def create_application(config=None):
    return Application(config or AppConfig.from_mapping(CFG, ROOT))


def _legacy_http_application():
    application = get_application()
    return application.http_application(routes=ROUTES)


Handler = bind_handler(_legacy_http_application)


class Server(HttpServer):
    def __init__(self, address, handler=Handler, *, application=None, **options):
        if application is not None:
            super().__init__(address, handler, application=application, **options)
            return
        owner = get_application()
        http = owner.http_application(routes=ROUTES)
        http.config = owner.config.with_port(address[1])
        super().__init__(address, handler, application=http, **options)
        owner.bind_port(self.server_address[1])
        http.config = owner.config
        CFG['port'] = self.server_address[1]


def cached(conn, name, query, compute):
    return get_application().cache.cached(conn, name, query, compute)


def clear_caches():
    get_application().cache.clear()


def schedule_local_services(conn):
    return get_application().local_services.schedule(conn)


def worker(stop, step, busy_wait, idle_wait):
    return get_application().supervisor.run(stop, step, busy_wait, idle_wait)


def start_workers(stop):
    return get_application().start_workers(stop)


def LLMPool(batch=None):
    return QualificationPool(get_application().qualification, batch=batch)


def repair_step(conn):
    return get_application().repair_step(conn)


def local_services_step(conn):
    return get_application().local_services_step(conn)


def background_qualify(conn):
    return get_application().background_qualify(conn)


def map_layout_step(conn):
    return get_application().map_layout_step(conn)


def permit_capable(*args, **kwargs):
    return get_application().collection.permit_capable(*args, **kwargs)


def collector_request(*args, **kwargs):
    return get_application().collection.collector_request(*args, **kwargs)


def collector_capable(*args, **kwargs):
    return get_application().collection.collector_capable(*args, **kwargs)


def ext_state(*args, **kwargs):
    return get_application().collection.ext_state(*args, **kwargs)


def benchmark_next(*args, **kwargs):
    return get_application().collection.benchmark_next(*args, **kwargs)


def benchmark_permit(*args, **kwargs):
    return get_application().collection.benchmark_permit(*args, **kwargs)


def benchmark_result(*args, **kwargs):
    return get_application().collection.benchmark_result(*args, **kwargs)


def ext_request(*args, **kwargs):
    return get_application().collection.ext_request(*args, **kwargs)


def ext_next(*args, **kwargs):
    return get_application().collection.ext_next(*args, **kwargs)


def check_seed_identity(*args, **kwargs):
    return get_application().collection.check_seed_identity(*args, **kwargs)


def stale_lease(*args, **kwargs):
    return get_application().collection.stale_lease(*args, **kwargs)


def trial_event_identity_matches(*args, **kwargs):
    return get_application().collection.trial_event_identity_matches(*args, **kwargs)


def stop_following_trial(*args, **kwargs):
    return get_application().collection.stop_following_trial(*args, **kwargs)


def ext_list_page(*args, **kwargs):
    return get_application().collection.ext_list_page(*args, **kwargs)


def _ext_list_page(*args, **kwargs):
    return get_application().collection._ext_list_page(*args, **kwargs)


def ext_profile(*args, **kwargs):
    return get_application().collection.ext_profile(*args, **kwargs)


def scoped_follower_redirect(*args, **kwargs):
    return get_application().collection.scoped_follower_redirect(*args, **kwargs)


def record_scraping_warning(*args, **kwargs):
    return get_application().collection.record_scraping_warning(*args, **kwargs)


def scraping_warning_url(*args, **kwargs):
    return get_application().collection.scraping_warning_url(*args, **kwargs)


def ext_error(*args, **kwargs):
    return get_application().collection.ext_error(*args, **kwargs)


def ext_heartbeat(*args, **kwargs):
    return get_application().collection.ext_heartbeat(*args, **kwargs)


def ext_aggregate(*args, **kwargs):
    return get_application().collection.ext_aggregate(*args, **kwargs)


def api_scraper(*args, **kwargs):
    return get_application().collection.api_scraper(*args, **kwargs)


def api_scraper_status(*args, **kwargs):
    return get_application().collection.api_scraper_status(*args, **kwargs)


def eta_hours(*args, **kwargs):
    return get_application().collection.eta_hours(*args, **kwargs)


def measured_rate(*args, **kwargs):
    return get_application().collection.measured_rate(*args, **kwargs)


def observed_per_minute(*args, **kwargs):
    return get_application().collection.observed_per_minute(*args, **kwargs)


def eta_with_budget(*args, **kwargs):
    return get_application().collection.eta_with_budget(*args, **kwargs)


def progress(*args, **kwargs):
    return get_application().collection.progress(*args, **kwargs)


def list_coverage(*args, **kwargs):
    return get_application().collection.list_coverage(*args, **kwargs)


def list_completion_reason(*args, **kwargs):
    return get_application().collection.list_completion_reason(*args, **kwargs)


def local_coverage(*args, **kwargs):
    return get_application().collection.local_coverage(*args, **kwargs)


def soak(*args, **kwargs):
    return get_application().collection.soak(*args, **kwargs)


def api_collection_suggestions(*args, **kwargs):
    return get_application().collection.api_collection_suggestions(*args, **kwargs)


def api_snowball(*args, **kwargs):
    return get_application().collection.api_snowball(*args, **kwargs)


def api_seeds(*args, **kwargs):
    return get_application().collection.api_seeds(*args, **kwargs)


def api_pause(*args, **kwargs):
    return get_application().collection.api_pause(*args, **kwargs)


def ai_left(*args, **kwargs):
    return get_application().collection.ai_left(*args, **kwargs)


def api_control(*args, **kwargs):
    return get_application().collection.api_control(*args, **kwargs)


def require_collection_resume(*args, **kwargs):
    return get_application().collection.require_collection_resume(*args, **kwargs)


def api_control_set(*args, **kwargs):
    return get_application().collection.api_control_set(*args, **kwargs)


def api_engine_start(*args, **kwargs):
    return get_application().collection.api_engine_start(*args, **kwargs)


def api_budget(*args, **kwargs):
    return get_application().collection.api_budget(*args, **kwargs)


def api_accounts(*args, **kwargs):
    return get_application().collection.api_accounts(*args, **kwargs)


def api_account_settings(*args, **kwargs):
    return get_application().collection.api_account_settings(*args, **kwargs)


def api_account_edit(*args, **kwargs):
    return get_application().collection.api_account_edit(*args, **kwargs)


def api_account_remove(*args, **kwargs):
    return get_application().collection.api_account_remove(*args, **kwargs)


def api_mobile_state(*args, **kwargs):
    return get_application().collection.api_mobile_state(*args, **kwargs)


def api_mobile_queue(*args, **kwargs):
    return get_application().collection.api_mobile_queue(*args, **kwargs)


def api_setup(*args, **kwargs):
    return get_application().collection.api_setup(*args, **kwargs)


def api_onboarding(*args, **kwargs):
    return get_application().collection.api_onboarding(*args, **kwargs)


def api_start(*args, **kwargs):
    return get_application().collection.api_start(*args, **kwargs)


def api_leads(*args, **kwargs):
    return get_application().leads.api_leads(*args, **kwargs)


def api_tags(*args, **kwargs):
    return get_application().leads.api_tags(*args, **kwargs)


def api_counts(*args, **kwargs):
    return get_application().leads.api_counts(*args, **kwargs)


def api_person(*args, **kwargs):
    return get_application().leads.api_person(*args, **kwargs)


def api_mark(*args, **kwargs):
    return get_application().leads.api_mark(*args, **kwargs)


def api_tag_edit(*args, **kwargs):
    return get_application().leads.api_tag_edit(*args, **kwargs)


def api_tag_rename(*args, **kwargs):
    return get_application().leads.api_tag_rename(*args, **kwargs)


def api_tag_delete(*args, **kwargs):
    return get_application().leads.api_tag_delete(*args, **kwargs)


def api_rules(*args, **kwargs):
    return get_application().leads.api_rules(*args, **kwargs)


def api_rule_add(*args, **kwargs):
    return get_application().leads.api_rule_add(*args, **kwargs)


def api_rule_preview(*args, **kwargs):
    return get_application().leads.api_rule_preview(*args, **kwargs)


def api_rule_delete(*args, **kwargs):
    return get_application().leads.api_rule_delete(*args, **kwargs)


def api_read(*args, **kwargs):
    return get_application().leads.api_read(*args, **kwargs)


def seed_links(*args, **kwargs):
    return get_application().maps.seed_links(*args, **kwargs)


def api_map(*args, **kwargs):
    return get_application().maps.api_map(*args, **kwargs)


def _map_database(*args, **kwargs):
    return get_application().maps._map_database(*args, **kwargs)


def api_map_view(*args, **kwargs):
    return get_application().maps.api_map_view(*args, **kwargs)


def api_map_search(*args, **kwargs):
    return get_application().maps.api_map_search(*args, **kwargs)


def api_map_edges(*args, **kwargs):
    return get_application().maps.api_map_edges(*args, **kwargs)


def api_map_overview(*args, **kwargs):
    return get_application().maps.api_map_overview(*args, **kwargs)


def api_connections(*args, **kwargs):
    return get_application().maps.api_connections(*args, **kwargs)


def map_graph(*args, **kwargs):
    return get_application().maps.map_graph(*args, **kwargs)


def api_biofetch_get(*args, **kwargs):
    return get_application().processing.api_biofetch_get(*args, **kwargs)


def api_biofetch(*args, **kwargs):
    return get_application().processing.api_biofetch(*args, **kwargs)


def api_qualify(*args, **kwargs):
    return get_application().processing.api_qualify(*args, **kwargs)


def api_llm(*args, **kwargs):
    return get_application().processing.api_llm(*args, **kwargs)


def llm_usage_report(*args, **kwargs):
    return get_application().processing.llm_usage_report(*args, **kwargs)


def api_llm_usage(*args, **kwargs):
    return get_application().processing.api_llm_usage(*args, **kwargs)


def api_llm_health(*args, **kwargs):
    return get_application().processing.api_llm_health(*args, **kwargs)


def api_llm_key_add(*args, **kwargs):
    return get_application().processing.api_llm_key_add(*args, **kwargs)


def api_llm_key_remove(*args, **kwargs):
    return get_application().processing.api_llm_key_remove(*args, **kwargs)


def api_llm_key_test(*args, **kwargs):
    return get_application().processing.api_llm_key_test(*args, **kwargs)


def api_scout(*args, **kwargs):
    return get_application().processing.api_scout(*args, **kwargs)


def api_scout_set(*args, **kwargs):
    return get_application().processing.api_scout_set(*args, **kwargs)


def api_llm_models_refresh(*args, **kwargs):
    return get_application().processing.api_llm_models_refresh(*args, **kwargs)


def api_llm_models(*args, **kwargs):
    return get_application().processing.api_llm_models(*args, **kwargs)


def api_processing_mode(*args, **kwargs):
    return get_application().processing.api_processing_mode(*args, **kwargs)


def notes_pending(*args, **kwargs):
    return get_application().processing.notes_pending(*args, **kwargs)


def api_note_retry(*args, **kwargs):
    return get_application().processing.api_note_retry(*args, **kwargs)


def local_processing_counts(*args, **kwargs):
    return get_application().processing.local_processing_counts(*args, **kwargs)


def engine_snapshot(*args, **kwargs):
    return get_application().processing.engine_snapshot(*args, **kwargs)


def api_engines(*args, **kwargs):
    return get_application().processing.api_engines(*args, **kwargs)


def api_k2_connection(*args, **kwargs):
    return get_application().processing.api_k2_connection(*args, **kwargs)


def api_k2_connection_test(*args, **kwargs):
    return get_application().processing.api_k2_connection_test(*args, **kwargs)


def api_local_processing(*args, **kwargs):
    return get_application().processing.api_local_processing(*args, **kwargs)


def _research_map(*args, **kwargs):
    return get_application().qualification._research_map(*args, **kwargs)


def refresh_network(*args, **kwargs):
    return get_application().qualification.refresh_network(*args, **kwargs)


def drain_network_dirty(*args, **kwargs):
    return get_application().qualification.drain_network_dirty(*args, **kwargs)


def laya_row(*args, **kwargs):
    return get_application().qualification.laya_row(*args, **kwargs)


def requalify(*args, **kwargs):
    return get_application().qualification.requalify(*args, **kwargs)


def qualify_batch(*args, **kwargs):
    return get_application().qualification.qualify_batch(*args, **kwargs)


def rebuild_laya_queue(*args, **kwargs):
    return get_application().qualification.rebuild_laya_queue(*args, **kwargs)


def laya_allowed(*args, **kwargs):
    return get_application().qualification.laya_allowed(*args, **kwargs)


def apply_local_review(*args, **kwargs):
    return get_application().qualification.apply_local_review(*args, **kwargs)


def local_processing_step(*args, **kwargs):
    return get_application().qualification.local_processing_step(*args, **kwargs)


def _local_processing_step(*args, **kwargs):
    return get_application().qualification._local_processing_step(*args, **kwargs)


def processing_maintenance(*args, **kwargs):
    return get_application().qualification.processing_maintenance(*args, **kwargs)


def laya_step(*args, **kwargs):
    return get_application().qualification.laya_step(*args, **kwargs)


def feedback_example(*args, **kwargs):
    return get_application().qualification.feedback_example(*args, **kwargs)


def fewshot(*args, **kwargs):
    return get_application().qualification.fewshot(*args, **kwargs)


def llm_candidates(*args, **kwargs):
    return get_application().qualification.llm_candidates(*args, **kwargs)


def current_local_review(*args, **kwargs):
    return get_application().qualification.current_local_review(*args, **kwargs)


def safe_research_lookup(*args, **kwargs):
    return get_application().qualification.safe_research_lookup(*args, **kwargs)


def research(*args, **kwargs):
    return get_application().qualification.research(*args, **kwargs)


def run_llm(*args, **kwargs):
    return get_application().qualification.run_llm(*args, **kwargs)


def llm_step(*args, **kwargs):
    return get_application().qualification.llm_step(*args, **kwargs)


def retag_if_changed(*args, **kwargs):
    return get_application().qualification.retag_if_changed(*args, **kwargs)


def refresh_laya_prefilter_if_changed(*args, **kwargs):
    return get_application().qualification.refresh_laya_prefilter_if_changed(*args, **kwargs)


def pfp_dir(*args, **kwargs):
    return get_application().photos.pfp_dir(*args, **kwargs)


def valid_pic(*args, **kwargs):
    return get_application().photos.valid_pic(*args, **kwargs)


def valid_pic_file(*args, **kwargs):
    return get_application().photos.valid_pic_file(*args, **kwargs)


def fetch_pic(*args, **kwargs):
    return get_application().photos.fetch_pic(*args, **kwargs)


def repair_pfp_cache(*args, **kwargs):
    return get_application().photos.repair_pfp_cache(*args, **kwargs)


def pfp_step(*args, **kwargs):
    return get_application().photos.pfp_step(*args, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=CFG['db'])
    parser.add_argument('--port', type=int, default=CFG['port'])
    parser.add_argument('--no-workers', action='store_true', help='Serve saved data without background work')
    args = parser.parse_args()
    config = AppConfig(str(Path(args.db).resolve()), args.port, ROOT,
                       saved_data_only=args.no_workers)
    application = Application(config)
    def terminate(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    try:
        application.serve(workers=not args.no_workers)
    except KeyboardInterrupt:
        pass
    finally:
        pending = application.close()
        if pending:
            print('External work still finishing: ' + ', '.join(pending), file=sys.stderr)


# Export tables before old direct callers inspect them. Construction starts no work.
get_application()

if __name__ == '__main__':
    main()
