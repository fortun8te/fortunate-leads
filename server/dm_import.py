"""Import summary evidence from a local Instagram message export.

Raw messages are parsed in memory and never written to SQLite or sent to an AI
service. An archive identifies a conversation, not a verified Instagram handle:
the owner must explicitly map each conversation to a handle at confirmation.
"""
import base64
import binascii
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone

import db
import workflows

MAX_UPLOAD = 32 * 1024 * 1024
MAX_JSON = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_CONVERSATION_FILES = 3000
MAX_MESSAGES = 200000
HANDLE = re.compile(r'^[A-Za-z0-9._]{1,30}$')
MESSAGE_FILE = re.compile(r'(^|/)messages/(inbox|message_requests)/[^/]+/message_\d+\.json$', re.I)


def _decode(item):
    if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('data_base64'), str):
        raise ValueError('Choose Instagram JSON files or one ZIP archive')
    name = item['name'].replace('\\', '/')
    if name.startswith('/') or '..' in name.split('/') or len(name) > 400:
        raise ValueError('Unsafe archive path')
    try:
        data = base64.b64decode(item['data_base64'], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('Invalid file data') from None
    if len(data) > MAX_UPLOAD:
        raise ValueError('File is too large (32 MB max)')
    return name, data


def _json_files(files):
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_CONVERSATION_FILES:
        raise ValueError('Choose up to 3,000 Instagram files')
    decoded = [_decode(x) for x in files]
    if sum(len(b) for _, b in decoded) > MAX_UPLOAD:
        raise ValueError('Selected files exceed 32 MB')
    result = []
    extracted_bytes = 0
    for name, data in decoded:
        if name.lower().endswith('.zip'):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    infos = archive.infolist()
                    if len(infos) > MAX_ENTRIES:
                        raise ValueError('ZIP contains too many files')
                    selected = [i for i in infos if MESSAGE_FILE.search(i.filename.replace('\\', '/')) and not i.is_dir()]
                    if len(selected) > MAX_CONVERSATION_FILES or sum(i.file_size for i in selected) > MAX_TOTAL:
                        raise ValueError('ZIP contains too many or too large message files')
                    for info in selected:
                        path = info.filename.replace('\\', '/')
                        if path.startswith('/') or '..' in path.split('/') or len(path) > 400:
                            raise ValueError('Unsafe ZIP path')
                        if not MESSAGE_FILE.search(path) or info.is_dir():
                            continue
                        if info.file_size > MAX_JSON or info.compress_size == 0 and info.file_size:
                            raise ValueError('A conversation JSON file is too large')
                        with archive.open(info) as stream:
                            content = stream.read(MAX_JSON + 1)
                        if len(content) > MAX_JSON:
                            raise ValueError('A conversation JSON file is too large')
                        extracted_bytes += len(content)
                        if extracted_bytes > MAX_TOTAL:
                            raise ValueError('ZIP message files exceed 64 MB')
                        result.append((path, content))
            except (zipfile.BadZipFile, RuntimeError, EOFError):
                raise ValueError('Could not read ZIP archive') from None
        elif name.lower().endswith('.json'):
            if len(data) > MAX_JSON:
                raise ValueError('A conversation JSON file is too large')
            # Standalone JSON selected from Finder can lose its parent path.
            result.append((name, data))
        else:
            raise ValueError('Choose Instagram JSON files or one ZIP archive')
    if not result:
        raise ValueError('No Instagram message JSON files found')
    return result


def _conversation_files(files):
    groups = {}
    for path, content in _json_files(files):
        try:
            value = json.loads(content.decode('utf-8-sig'))
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError('Invalid Instagram message JSON') from None
        if not isinstance(value, dict) or not isinstance(value.get('participants'), list) or not isinstance(value.get('messages'), list):
            # Other Instagram JSON files may be selected in a full download.
            continue
        names = []
        for p in value['participants']:
            if not isinstance(p, dict) or not isinstance(p.get('name'), str) or not p['name'].strip():
                raise ValueError('A conversation has missing participants')
            names.append(p['name'].strip())
        if len(names) != 2 or len({x.casefold() for x in names}) != 2:
            # A group DM or duplicate display name cannot identify one person.
            key = ('group', path.rsplit('/', 1)[0] if '/' in path else path)
        else:
            key = ('pair', path.rsplit('/', 1)[0] if '/' in path else tuple(sorted(x.casefold() for x in names)))
        group = groups.setdefault(key, {'participants': names, 'messages': [], 'paths': set(), 'group': key[0] == 'group'})
        if sorted(x.casefold() for x in group['participants']) != sorted(x.casefold() for x in names):
            raise ValueError('Conversation files have conflicting participants')
        if path in group['paths']:
            raise ValueError('The same conversation file was selected twice')
        group['paths'].add(path)
        group['messages'].extend(value['messages'])
        if sum(len(x['messages']) for x in groups.values()) > MAX_MESSAGES:
            raise ValueError('Archive has too many messages')
    if not groups:
        raise ValueError('No supported Instagram conversations found')
    return groups.values()


def _stamp(message):
    raw = message.get('timestamp_ms', message.get('timestamp'))
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        n = float(raw)
        if not 0 <= n <= 4102444800000:
            return None
        if n > 4102444800:
            n /= 1000
        return datetime.fromtimestamp(n, timezone.utc).isoformat(timespec='seconds')
    except (ValueError, OverflowError, OSError):
        return None


def analyze(files, owner_name=None):
    if owner_name is not None and (not isinstance(owner_name, str) or len(owner_name) > 200):
        raise ValueError('Choose your name exactly as shown in the export')
    owner = owner_name.strip().casefold() if owner_name else None
    senders = set()
    threads = []
    skipped_groups = 0
    for group in _conversation_files(files):
        participants = group['participants']
        valid = []
        for msg in group['messages']:
            if not isinstance(msg, dict) or not isinstance(msg.get('sender_name'), str):
                continue
            sender = msg['sender_name'].strip()
            senders.add(sender)
            stamp = _stamp(msg)
            if stamp:
                valid.append((sender, stamp))
        if group['group']:
            skipped_groups += 1
            continue
        if owner is None or sum(p.casefold() == owner for p in participants) != 1:
            continue
        counterpart = next(p for p in participants if p.casefold() != owner)
        outbound = [s for sender, s in valid if sender.casefold() == owner]
        inbound = [s for sender, s in valid if sender.casefold() == counterpart.casefold()]
        if not outbound and not inbound:
            continue
        # Stable across archive re-exports; raw message text is not retained.
        metadata = [sorted(p.casefold() for p in participants), sorted((sender.casefold(), stamp) for sender, stamp in valid)]
        fingerprint = hashlib.sha256(json.dumps(metadata, separators=(',', ':')).encode()).hexdigest()
        threads.append({'fingerprint': fingerprint, 'participant': counterpart,
                        'messages': len(valid), 'outbound': len(outbound), 'inbound': len(inbound),
                        'first_at': min(s for _, s in valid), 'last_at': max(s for _, s in valid),
                        'first_outbound_at': min(outbound) if outbound else None,
                        'last_outbound_at': max(outbound) if outbound else None})
    threads.sort(key=lambda x: (x['participant'].casefold(), x['fingerprint']))
    return {'owner_name': owner_name or '', 'sender_candidates': sorted(senders, key=str.casefold),
            'threads': threads, 'skipped_group_threads': skipped_groups,
            'outbound_threads': sum(t['outbound'] > 0 for t in threads),
            'inbound_only_threads': sum(t['outbound'] == 0 for t in threads)}


def preview(conn, q, body):
    return analyze(body.get('files'), body.get('owner_name'))


def confirm(conn, q, body):
    owner_name = body.get('owner_name')
    if not isinstance(owner_name, str) or not owner_name.strip():
        raise ValueError('Choose your name exactly as shown in the export')
    result = analyze(body.get('files'), owner_name)
    mappings = body.get('mappings')
    if not isinstance(mappings, dict):
        raise ValueError('Confirm Instagram handles for the conversations you want to import')
    chosen = []
    for thread in result['threads']:
        handle = mappings.get(thread['fingerprint'])
        if not handle:
            continue
        if not isinstance(handle, str) or not HANDLE.fullmatch(handle.lstrip('@')):
            raise ValueError('Enter a valid Instagram username')
        if not thread['outbound']:
            raise ValueError('Inbound-only conversations cannot be marked Contacted')
        chosen.append((thread, handle.lstrip('@').lower()))
    if not chosen:
        raise ValueError('Map at least one outbound conversation to an Instagram username')
    import server  # late import: server loads this module for route registration
    created = updated = unchanged = 0
    contacts = []
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        for thread, handle in chosen:
            row = conn.execute('SELECT id FROM people WHERE handle=? COLLATE NOCASE', (handle,)).fetchone()
            if row:
                pid = row['id']
            else:
                pid = db.upsert_person(conn, {'handle': handle})
                created += 1
            contacts.append({'id': pid, 'handle': handle})
            prior = conn.execute("SELECT after_value FROM activity WHERE person_id=? AND kind='dm_import'", (pid,)).fetchall()
            if any((json.loads(r['after_value'] or '{}').get('fingerprint') == thread['fingerprint']) for r in prior):
                unchanged += 1
                continue
            metadata = {k: thread[k] for k in ('fingerprint', 'messages', 'outbound', 'inbound', 'first_at', 'last_at', 'first_outbound_at', 'last_outbound_at')}
            metadata['source'] = 'instagram_export'
            workflows.event(conn, pid, 'dm_import', body='Instagram DM history imported', after=metadata,
                            happened_at=thread['last_outbound_at'])
            status = conn.execute('SELECT status FROM marks WHERE person_id=?', (pid,)).fetchone()
            if not status or status['status'] in (None, 'interested'):
                server.set_status(conn, [pid], status='contacted')
                updated += 1
            else:
                unchanged += 1
    return {'imported_threads': len(chosen), 'new_people': created,
            'marked_contacted': updated, 'preserved_existing_stage': unchanged,
            'contacts': list({c['id']: c for c in contacts}.values())}


def routes(_server):
    return [('POST', r'/api/dm-import/preview', preview),
            ('POST', r'/api/dm-import/confirm', confirm)]
