#!/usr/bin/env python3
"""Run the V2 application on a virtual clock, with manual local ticks only.

X-Sim-Now supplies monotonic epoch milliseconds for the application and storage
clock owners. GET /__sim/tick runs rules qualification and profile planning once.
The simulator never calls the production entry point or starts its workers,
local services, photo downloader, browser launcher or external AI jobs.

Usage: python3 tests/e2e/sim_server.py --db /tmp/x.sqlite --port 18777
FL_SERVER_DIR may select another V2 server package for comparison.
"""

import argparse
import importlib
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = Path(os.environ.get('FL_SERVER_DIR') or ROOT / 'server').resolve()
if not (SERVER_DIR / 'backend' / 'app.py').is_file():
    raise RuntimeError('The simulator requires a V2 server package; use the original simulator for legacy snapshots')
sys.path.insert(0, str(SERVER_DIR))

from backend.app import Application  # noqa: E402
from backend.common import AppConfig  # noqa: E402
from backend.profile_planner import plan_profiles  # noqa: E402
from backend_http import Handler, Server  # noqa: E402

LOCK = threading.Lock()
VNOW = {'ms': None}


class SimDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        milliseconds = VNOW['ms']
        if milliseconds is None:
            return datetime.now(tz)
        stamp = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
        return stamp.astimezone(tz) if tz else datetime.fromtimestamp(milliseconds / 1000)


def install_clock():
    """Patch every owning alias, including functions re-exported by db.py.

    Patching only the old server/db facade leaves V2's actual functions using
    wall time. Shared supporting modules such as accounts and control also own
    datetime aliases. Real socket deadlines and time.monotonic stay real.
    """
    owners = (
        'backend.common', 'backend.collection', 'backend.processing',
        'backend.evidence', 'backend.qualification', 'backend.profile_planner',
        'backend.queries', 'backend.maps', 'storage.clock', 'storage.identity',
        'storage.evidence', 'storage.checkpoints', 'db',
    )
    for name in owners:
        importlib.import_module(name)
    for module in tuple(sys.modules.values()):
        location = getattr(module, '__file__', None)
        if not location:
            continue
        try:
            owned = Path(location).resolve().is_relative_to(SERVER_DIR)
        except (OSError, TypeError, ValueError):
            continue
        if owned and getattr(module, 'datetime', None) is datetime:
            module.datetime = SimDateTime


def forbidden_worker_start(*args, **kwargs):
    raise RuntimeError('Automatic workers and external services are disabled in the simulator')


class SimulationApplication(Application):
    """Structurally forbid production work, including accidental future calls."""

    start_workers = forbidden_worker_start
    serve = forbidden_worker_start

    def __init__(self, config):
        super().__init__(config)
        self.supervisor.start = forbidden_worker_start
        # Ordinary collection Resume also asks local services to reconcile.
        # Keep its existing temporary-workspace no-op result without starting
        # anything, so simulator controls retain their normal response bodies.
        self.local_services.schedule = lambda *args, **kwargs: False
        self.local_services.step = forbidden_worker_start

    def tick(self, connection, query, body):
        return {'qualified': self.qualification.qualify_batch(connection, 5000),
                'planned': plan_profiles(connection)}


class SimulationHandler(Handler):
    def route(self, method):
        value = self.headers.get('X-Sim-Now')
        if value:
            try:
                milliseconds = int(value)
            except ValueError:
                return self.send(400, {'ok': False, 'error': 'Invalid simulation clock'})
            with LOCK:
                VNOW['ms'] = max(VNOW['ms'] or 0, milliseconds)
        return super().route(method)


def create_application(database, port):
    database = Path(database).resolve()
    # The simulator must never initialize a saved workspace even when invoked
    # with an incorrect command. The driver always creates an fl-e2e temp dir.
    temporary_roots = (Path(tempfile.gettempdir()).resolve(), Path('/tmp').resolve())
    if not any(database.is_relative_to(root) for root in temporary_roots):
        raise ValueError('The simulator database must be inside a temporary directory')
    if port == 8777:
        raise ValueError('The simulator must use a separate port')
    install_clock()
    application = SimulationApplication(AppConfig(str(database), port, SERVER_DIR.parent))
    application.initialize()
    application.http = application.http_application(
        [*application.routes, ('GET', '/__sim/tick', application.tick)])
    return application


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--port', type=int, required=True)
    arguments = parser.parse_args()
    application = create_application(arguments.db, arguments.port)
    application.server = Server(('127.0.0.1', arguments.port), SimulationHandler,
                                application=application.http)
    try:
        print(f'Simulated Fortunate Leads on port {application.server.server_address[1]}; '
              'automatic workers disabled', flush=True)
        application.server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        application.close()


if __name__ == '__main__':
    main()
