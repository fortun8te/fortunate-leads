"""Cookie-free public bio collector. Durable ownership belongs to server/jobs, not the scraper cache."""
import argparse
import hashlib
import os
import fcntl
import asyncio
import signal
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'server'), str(ROOT / 'scraper/app')]
import db
import public_bios as bridge
from igscraper import ProfileScraper, ScraperConfig
from igscrape_app.egress_config import load_yaml_egresses


async def run(path):
    # One owner per database; separate workspaces must not block each other.
    db_path = str(Path(path).resolve())
    lock_name = hashlib.sha256(db_path.encode()).hexdigest()[:24]
    lock = open(ROOT / 'data' / f'public-bios-worker-{lock_name}.lock', 'a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return
    parent_pid = os.getppid()
    conn = None
    lane = 'public-bios:' + uuid.uuid4().hex
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = set()
    try:
        conn = db.connect(db_path)
        bridge.init(conn)
        # The OS releases flock on an ungraceful exit. With exclusive ownership,
        # any public-worker lease left here belongs to a dead predecessor.
        stale_lanes = conn.execute("SELECT DISTINCT lane FROM jobs WHERE state='leased' "
                                   "AND lane LIKE 'public-bios:%'").fetchall()
        for stale in stale_lanes:
            bridge.release(conn, stale['lane'])
        cfg = bridge.settings(conn)

        def permitted():
            return (not stop.is_set() and os.getppid() == parent_pid
                    and bridge.enabled(conn) and bridge.settings(conn) == cfg)

        if not permitted():
            return
        # Deliberately do not call from_env: public collection never inherits personal account cookies.
        egresses = load_yaml_egresses(bridge.config_path(cfg))
        if not egresses:
            raise ValueError('No configured egresses')
        config = ScraperConfig(egresses=egresses, concurrency=cfg['concurrency'], sessionid=None, cookies_file=None,
                               cache_path=str(ROOT / 'data/public-bios-cache.sqlite'), health_interval=0,
                               max_retries=1, max_park_wait=30, max_inflight_per_egress=2)
        async with ProfileScraper(config) as scraper:
            bridge.restore_cooldowns(conn, scraper._pool)
            # Persist immediately when transport records a wait, including a server Retry-After.
            # A process restart or pause must not clear the network's requested cooldown.
            for method in ('block', 'transient', 'network_failure'):
                original = getattr(scraper._pool, method)
                def record_wait(*args, _original=original, **kwargs):
                    result = _original(*args, **kwargs)
                    bridge.save_cooldowns(conn, scraper._pool)
                    return result
                setattr(scraper._pool, method, record_wait)
            # Re-check the shared pause before every fallback request as well as before each profile.
            fetch_get = scraper._fetch.get
            async def permitted_get(*args, **kwargs):
                if not permitted():
                    raise asyncio.CancelledError()
                return await fetch_get(*args, **kwargs)
            scraper._fetch.get = permitted_get
            async def collect(job):
                if not permitted():
                    return
                def save_item(item):
                    # Import synchronously at delivery, before the scraper's outer
                    # gather can be cancelled. Complete evidence survives a pause.
                    try:
                        bridge.import_item(conn, job, item)
                    except BaseException:
                        # Other tasks share this connection. Roll back before yielding
                        # so their imports/cooldown writes cannot commit partial work.
                        conn.rollback()
                        raise
                await scraper.run([job['handle']], on_item=save_item,
                                  concurrency=1, collect=False)

            try:
                while permitted():
                    done = {t for t in tasks if t.done()}
                    for task in done:
                        try:
                            task.result()
                        except asyncio.CancelledError:
                            if permitted():
                                raise
                        except Exception:
                            conn.rollback()  # do not let cooldown persistence commit a partial import
                            raise
                    tasks -= done
                    bridge.plan(conn)
                    while len(tasks) < cfg['concurrency'] and permitted():
                        job = bridge.lease(conn, lane)
                        if not job:
                            break
                        if not permitted():
                            break  # finally releases the lease before another worker takes it
                        tasks.add(asyncio.create_task(collect(job)))
                    if not permitted():
                        break
                    bridge.renew(conn, lane)
                    db.set_setting(conn, 'public_bios_heartbeat', {'epoch': time.time(), 'inflight': len(tasks),
                                   'healthyEgresses': scraper.healthy_count(), 'egresses': len(egresses)})
                    db.set_setting(conn, 'public_bios_error', None)
                    conn.commit()
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=.5)
                    except asyncio.TimeoutError:
                        pass
            finally:
                for task in tasks:
                    task.cancel()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                if any(isinstance(item, Exception) for item in results):
                    conn.rollback()
                bridge.save_cooldowns(conn, scraper._pool)
    except Exception:
        # Exceptions can embed proxy passwords or remote response data. Only expose a generic operational error.
        if conn is not None:
            # A failed import may have left an open transaction on this shared connection.
            conn.rollback()
            db.set_setting(conn, 'public_bios_error', 'Public collector stopped unexpectedly; verify the egress configuration and runtime.')
            conn.commit()
        raise
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if conn is not None:
                try:
                    conn.rollback()
                    bridge.release(conn, lane)
                    db.set_setting(conn, 'public_bios_heartbeat', {'epoch': 0, 'inflight': 0})
                    conn.commit()
                finally:
                    conn.close()
        finally:
            lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    asyncio.run(run(parser.parse_args().db))
