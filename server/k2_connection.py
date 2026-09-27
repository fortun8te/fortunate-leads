"""Dormant connection choice for the pinned K2 lane.

Only literal RFC1918 IPv4 peers are accepted. This avoids DNS rebinding, proxying,
and accidentally sending private prompts to a public endpoint.
"""
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import re

CONFIG_PATH = Path.home() / 'Library/Application Support/Fortunate Leads/k2/connection.json'
DEFAULT = {'location': 'this_mac', 'host': '', 'port': 11436}


def validate(value):
    if not isinstance(value, dict) or set(value) != {'location', 'host', 'port'}:
        raise ValueError('Connection needs location, host, and port')
    location, host, port = value['location'], value['host'], value['port']
    if location == 'this_mac':
        if host not in ('', '127.0.0.1') or port != 11436:
            raise ValueError('This Mac uses the fixed local K2 address')
        return dict(DEFAULT)
    if location != 'other_pc' or not isinstance(host, str) or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Choose another PC with a private IPv4 address and valid port')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError('Use the other PC private IPv4 address') from None
    if not isinstance(ip, ipaddress.IPv4Address) or not any(ip in network for network in (
            ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('172.16.0.0/12'),
            ipaddress.ip_network('192.168.0.0/16'))):
        raise ValueError('Use a private LAN IPv4 address')
    return {'location': location, 'host': str(ip), 'port': port}


def read():
    stored = read_secret()
    return {key: stored[key] for key in DEFAULT}


def read_secret():
    try:
        stored = json.loads(CONFIG_PATH.read_text())
        public = validate({key: stored[key] for key in DEFAULT})
        secret = stored.get('api_key', '')
        if not isinstance(secret, str) or (secret and not re.fullmatch(r'[!-~]{1,512}', secret)):
            raise ValueError('Invalid saved K2 key')
        return dict(public, api_key=secret)
    except FileNotFoundError:
        return dict(DEFAULT, api_key='')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError('Stored K2 connection is invalid') from exc


def save(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid K2 connection')
    config = validate({key: val for key, val in value.items() if key != 'api_key'})
    key = value.get('api_key')
    if key is None:
        try:
            previous = read_secret()
        except ValueError:
            previous = dict(DEFAULT, api_key='')
        key = previous['api_key'] if all(previous[field] == config[field] for field in DEFAULT) else ''
    if not isinstance(key, str) or (key and not re.fullmatch(r'[!-~]{1,512}', key)):
        raise ValueError('Invalid K2 API key')
    stored = dict(config, api_key=key)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.connection-', dir=CONFIG_PATH.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(stored, stream, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, CONFIG_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return config


def endpoint(config=None):
    config = read() if config is None else validate(config)
    if config['location'] == 'this_mac':
        return 'http://127.0.0.1:11436'
    return 'http://%s:%d' % (config['host'], config['port'])
