"""Photo cache integrity and bounded fetches for one application instance."""

from datetime import timedelta
import hashlib
import os
from pathlib import Path
import sqlite3
import ssl
import struct
import tempfile
import threading
import urllib.request
from urllib.parse import urlparse

import db
import map_view
import meta_network

from backend_http import IMAGE_CACHE_CONTROL

from .common import Bad, PIC_HOSTS, PIC_MAX, csv, iso

# One map page of photos in few requests. Body: b'MPH1', count (u32 LE), then count pairs of
# (person id u32, length u32), then the photo bytes back to back in that order. Length 0 means the
# person has no saved photo; PHOTO_DEFERRED means it was left out to stay within PHOTO_BATCH_BYTES,
# so ask again.
PHOTO_BATCH_MAX = 256
PHOTO_BATCH_BYTES = 6 * 1024 * 1024
PHOTO_DEFERRED = 0xFFFFFFFF

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


class PictureOwnership:
    """Reuse SQLite's parsed schema, but read current picture ownership on every request.

    Serving a map's worth of photos asked this once per image on a fresh connection, which cost
    more than the image itself (about 6 ms of setup against a 1 ms file). One shared read-only
    connection answers the primary-key lookup in microseconds and follows a replaced database file.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.conn = None
        self.identity = None

    def close(self):
        with self.lock:
            if self.conn is not None:
                self.conn.close()
            self.conn = None
            self.identity = None

    def owns(self, path, pid):
        with self.lock:
            path = Path(path).resolve()
            stat = path.stat()
            identity = (str(path), stat.st_dev, stat.st_ino)
            if identity != self.identity:
                if self.conn is not None:
                    self.conn.close()
                self.conn = None
                self.identity = None
                # A fresh autocommit SELECT sees committed ownership changes, including WAL
                # updates. Do not use immutable mode or cache the query result.
                self.conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15,
                                            isolation_level=None, check_same_thread=False)
                self.identity = identity
            # Look up the primary key first; combining the filename predicate can select a much
            # slower index on large saved-profile databases.
            cursor = self.conn.execute('SELECT pic_file FROM people WHERE id=?', (pid,))
            try:
                row = cursor.fetchone()
                return row is not None and row[0] == f'{pid}.jpg'
            finally:
                cursor.close()


class PhotoService:
    def __init__(self, config):
        self.config = config
        self.check_id = 0
        self._pic_opener = None
        self._ownership = PictureOwnership()

    def owns(self, person_id):
        """Whether the database still associates this person's photo file with them."""
        return self._ownership.owns(self.config.db, person_id)

    def close(self):
        self._ownership.close()

    def api_batch(self, conn, q, body):
        """GET /api/map/photos?ids=1,2,3: many saved photos in one response (see PHOTO_BATCH_MAX)."""
        ids = []
        for text in csv(q, 'ids'):
            if not text.isascii() or not text.isdigit() or int(text) >= 2 ** 32:
                raise Bad('ids must be whole numbers')
            if int(text) not in ids:
                ids.append(int(text))
        if not ids or len(ids) > PHOTO_BATCH_MAX:
            raise Bad(f'ids must list 1 to {PHOTO_BATCH_MAX} people')
        owned = {row[0] for row in conn.execute(
            f"SELECT id FROM people WHERE id IN ({','.join('?' * len(ids))}) AND pic_file=id||'.jpg'", ids)}
        directory = self.pfp_dir()
        table, parts, total, digest = [], [], 0, hashlib.blake2b(digest_size=12)
        for person_id in ids:
            data = b''
            if person_id in owned:
                try:
                    with (directory / f'{person_id}.jpg').open('rb') as handle:
                        info = os.fstat(handle.fileno())
                        if parts and total + info.st_size > PHOTO_BATCH_BYTES:
                            table.append((person_id, PHOTO_DEFERRED))
                            continue
                        data = handle.read(PIC_MAX + 1)
                except OSError:
                    data = b''
                if data and self.valid_pic(data):
                    digest.update(struct.pack('<IQQ', person_id, info.st_mtime_ns, info.st_size))
                else:
                    data = b''
            table.append((person_id, len(data)))
            if data:
                parts.append(data)
                total += len(data)
        head = b'MPH1' + struct.pack('<I', len(table)) + b''.join(struct.pack('<II', *row) for row in table)
        return map_view.Raw(head + b''.join(parts), f'"{digest.hexdigest()}"',
                            headers={'Content-Type': 'application/octet-stream', 'Cache-Control': IMAGE_CACHE_CONTROL})

    @property
    def pic_opener(self):
        if self._pic_opener is None:
            context = ssl.create_default_context(
                cafile='/etc/ssl/cert.pem' if Path('/etc/ssl/cert.pem').is_file() else None)
            self._pic_opener = urllib.request.build_opener(
                NoRedirect, urllib.request.HTTPSHandler(context=context))
        return self._pic_opener


    def pfp_dir(self):
        return Path(self.config.db).resolve().parent / 'pfp'

    def valid_pic(self, data):
        if not data or len(data) > PIC_MAX:
            return False
        if data[:3] == b'\xff\xd8\xff':
            return data.endswith(b'\xff\xd9')
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return len(data) >= 20 and data[-8:-4] == b'IEND'
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return len(data) >= 20 and int.from_bytes(data[4:8], 'little') == len(data) - 8
        return False

    def valid_pic_file(self, path):
        try:
            size = path.stat().st_size
            if size < 12 or size > PIC_MAX:
                return False
            with path.open('rb') as f:
                head = f.read(12)
                f.seek(-12, os.SEEK_END)
                tail = f.read(12)
        except OSError:
            return False
        return ((head[:3] == b'\xff\xd8\xff' and tail[-2:] == b'\xff\xd9')
                or (head[:8] == b'\x89PNG\r\n\x1a\n' and tail[4:8] == b'IEND')
                or (head[:4] == b'RIFF' and head[8:12] == b'WEBP'
                    and int.from_bytes(head[4:8], 'little') == size - 8))

    def fetch_pic(self, url):
        if not isinstance(url, str):
            return None
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ''
            port = parsed.port
        except ValueError:
            return None
        if parsed.scheme != 'https' or not host.endswith(PIC_HOSTS) or parsed.username or parsed.password or port not in (None, 443):
            return None
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with self.pic_opener.open(req, timeout=10) as r:
                if not (urlparse(r.geturl()).hostname or '').endswith(PIC_HOSTS):
                    return None
                data = r.read(PIC_MAX + 1)
        except (OSError, ValueError):
            return None
        return data if self.valid_pic(data) else None

    def repair_pfp_cache(self, conn, limit=32):
        """Reconcile a bounded local slice of associated photos without HTTP.

        The returned change count and cursor allow an explicit one-pass recovery.
        This also runs during collection pauses and network holds.
        """
        rows = conn.execute('SELECT id, pic_file FROM people WHERE id > ? ORDER BY id LIMIT ?',
                            (self.check_id, max(1, min(int(limit), 1000)))).fetchall()
        if not rows:
            self.check_id = 0
            return 0
        repaired = 0
        directory = self.pfp_dir()
        for row in rows:
            self.check_id = row['id']
            filename = f"{row['id']}.jpg"
            valid = self.valid_pic_file(directory / filename)
            # A file without a current DB association has no ownership proof.
            if row['pic_file'] and (not valid or row['pic_file'] != filename):
                repaired += conn.execute('UPDATE people SET pic_file=NULL, pic_refresh=0 '
                                         'WHERE id=? AND pic_file=?',
                                         (row['id'], row['pic_file'])).rowcount
        if repaired:
            conn.commit()
        return repaired

    def pfp_step(self, conn):
        repaired = self.repair_pfp_cache(conn)
        if meta_network.blocked(conn):
            return bool(repaired)
        r = conn.execute('SELECT p.id,p.ig_id,p.handle,p.pic_url,p.pic_file,p.pic_attempts FROM people p '
                         "WHERE p.pic_url IS NOT NULL AND (coalesce(p.pic_file,'')='' OR p.pic_refresh=1) "
                         'AND (p.pic_retry_at IS NULL OR p.pic_retry_at<=?) '
                         'ORDER BY (p.pic_file IS NULL) DESC,p.updated_at DESC LIMIT 1', (db.now(),)).fetchone()
        if not r or meta_network.blocked(conn):
            return bool(repaired)
        data = self.fetch_pic(r['pic_url'])
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        current = conn.execute('SELECT * FROM people WHERE id=?', (r['id'],)).fetchone()
        if not current or any(current[k] != r[k] for k in ('ig_id', 'handle', 'pic_url')):
            if current:
                conn.execute('UPDATE people SET pic_refresh=1 WHERE id=? AND pic_url IS NOT NULL', (r['id'],))
            conn.commit()
            return True
        directory = self.pfp_dir()
        filename = f"{r['id']}.jpg"
        if data:
            directory.mkdir(parents=True, exist_ok=True)
            name = None
            try:
                with tempfile.NamedTemporaryFile(dir=directory, prefix=f".{r['id']}.", suffix='.tmp', delete=False) as tmp:
                    name = tmp.name
                    tmp.write(data)
                os.replace(name, directory / filename)
            finally:
                if name and os.path.exists(name):
                    os.unlink(name)
            conn.execute('UPDATE people SET pic_file=?,pic_refresh=0,pic_attempts=0,pic_retry_at=NULL WHERE id=?',
                         (filename, r['id']))
        else:
            attempts = min(current['pic_attempts'] + 1, 12)
            retry_at = iso(db.utc_now() + timedelta(seconds=min(86400, 60 * 2 ** (attempts - 1))))
            valid = current['pic_file'] == filename and self.valid_pic_file(directory / filename)
            conn.execute('UPDATE people SET pic_file=?,pic_refresh=1,pic_attempts=?,pic_retry_at=? WHERE id=?',
                         (filename if valid else '', attempts, retry_at, r['id']))
        conn.commit()
        return True
