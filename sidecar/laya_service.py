#!/usr/bin/env python3
"""Opt-in LaunchAgent management for the local Laya sidecar.

This script never downloads weights. Install writes a plist; start is the only
command that launches the model process. Run it from the deployed checkout.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import laya_server

LABEL = 'com.fortunate.laya'
HERE = Path(__file__).resolve().parent
PLIST = Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')
LOG = Path.home() / 'Library' / 'Logs' / 'FortunateLeads' / 'laya.log'


def config():
    port = int(os.environ.get('LAYA_PORT', laya_server.DEFAULT_PORT))
    if not 1 <= port <= 65535:
        raise ValueError('LAYA_PORT must be between 1 and 65535')
    if laya_server.BATCH_SIZE <= 0:
        raise ValueError('LAYA_BATCH_SIZE must be positive')
    device = os.environ.get('LAYA_DEVICE') or None
    if device is not None and device not in ('cpu', 'mps', 'cuda'):
        raise ValueError('LAYA_DEVICE must be cpu, mps or cuda')
    cache = Path(os.environ.get('HF_HOME', str(HERE / '.cache' / 'huggingface'))).expanduser().resolve()
    return {'port': port, 'model': laya_server.DEFAULT_MODEL,
            'deployment_version': laya_server.DEPLOYMENT_VERSION,
            'batch_size': laya_server.BATCH_SIZE, 'cache': cache, 'device': device}


def service_spec(cfg=None):
    cfg = config() if cfg is None else cfg
    env = {'HF_HOME': str(cfg['cache']), 'HF_HUB_OFFLINE': '1',
           'LAYA_MODEL': cfg['model'], 'LAYA_DEPLOYMENT_VERSION': cfg['deployment_version'],
           'LAYA_PORT': str(cfg['port']), 'LAYA_BATCH_SIZE': str(cfg['batch_size'])}
    if cfg.get('device'):
        env['LAYA_DEVICE'] = cfg['device']
    return {'Label': LABEL,
            'ProgramArguments': [str(HERE / '.venv' / 'bin' / 'python'), str(HERE / 'laya_server.py'),
                                 '--port', str(cfg['port']), '--model', cfg['model']],
            'WorkingDirectory': str(HERE.parent),
            'EnvironmentVariables': env,
            'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 30,
            'StandardOutPath': str(LOG), 'StandardErrorPath': str(LOG)}


def preflight(cfg):
    python = HERE / '.venv' / 'bin' / 'python'
    if not python.is_file():
        raise RuntimeError('Sidecar environment is missing; run sh sidecar/setup.sh first')
    check = subprocess.run([str(python), '-c', 'from importlib.metadata import version; print(version("laya"))'],
                           capture_output=True, text=True, timeout=15)
    if check.returncode or check.stdout.strip() != laya_server.LAYA_VERSION:
        raise RuntimeError('Sidecar needs the pinned laya==%s package' % laya_server.LAYA_VERSION)
    if not cfg['cache'].is_dir():
        raise RuntimeError('Model cache is missing at %s; this service uses cached weights only' % cfg['cache'])
    model, _, subfolder = cfg['model'].partition(':')
    if Path(model).is_dir():
        snapshots = [Path(model)]
    else:
        snapshots = (cfg['cache'] / 'hub' / ('models--' + model.replace('/', '--')) / 'snapshots').glob('*')
    needed = ('rl_agent_config.json', 'model.safetensors', 'tokenizer/tokenizer.json', 'encoder/config.json')
    if not any(all((snapshot / subfolder / name).is_file() for name in needed) for snapshot in snapshots):
        raise RuntimeError('Cached checkpoint files for %s are missing in %s' % (cfg['model'], cfg['cache']))


def install(replace=False):
    cfg = config()
    preflight(cfg)
    spec = service_spec(cfg)
    body = plistlib.dumps(spec)
    if PLIST.exists():
        if PLIST.read_bytes() == body:
            return 'already installed'
        if not replace:
            raise RuntimeError('%s already exists; use install --replace after reviewing it' % PLIST)
        owned_spec()
        if loaded():
            raise RuntimeError('stop the current Laya service before replacing its plist')
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=PLIST.parent, prefix=LABEL + '.', suffix='.tmp', delete=False) as tmp:
        tmp.write(body)
        name = tmp.name
    try:
        os.replace(name, PLIST)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return 'installed (not started)'


def target():
    return 'gui/%d/%s' % (os.getuid(), LABEL)


def loaded():
    return subprocess.run(['launchctl', 'print', target()], capture_output=True).returncode == 0


def launch(*args):
    run = subprocess.run(['launchctl', *args], capture_output=True, text=True)
    if run.returncode:
        raise RuntimeError(run.stderr.strip() or run.stdout.strip() or 'launchctl failed')


def owned_spec():
    if not PLIST.exists():
        raise RuntimeError('Laya service is not installed; run install first')
    spec = plistlib.loads(PLIST.read_bytes())
    if spec.get('Label') != LABEL:
        raise RuntimeError('installed plist has an unexpected service label')
    args = spec.get('ProgramArguments') or []
    if len(args) < 2 or args[0] != str(HERE / '.venv' / 'bin' / 'python') or args[1] != str(HERE / 'laya_server.py'):
        raise RuntimeError('installed Laya service belongs to a different checkout')
    return spec


def installed_config():
    spec = owned_spec()
    args = spec['ProgramArguments']
    env = spec.get('EnvironmentVariables') or {}
    try:
        cfg = {'port': int(env['LAYA_PORT']), 'model': env['LAYA_MODEL'],
               'deployment_version': env['LAYA_DEPLOYMENT_VERSION'], 'cache': Path(env['HF_HOME']),
               'device': env.get('LAYA_DEVICE')}
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError('installed Laya plist is missing required configuration') from error
    if cfg['device'] not in (None, 'cpu', 'mps', 'cuda'):
        raise RuntimeError('installed Laya plist has an unsupported device')
    if env.get('HF_HUB_OFFLINE') != '1' or args != [str(HERE / '.venv' / 'bin' / 'python'),
            str(HERE / 'laya_server.py'), '--port', str(cfg['port']), '--model', cfg['model']]:
        raise RuntimeError('installed Laya plist differs from the supported offline service')
    return cfg


def health_response(cfg, timeout=3):
    url = 'http://127.0.0.1:%d/health' % cfg['port']
    req = urllib.request.Request(url)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as response:
            data = json.loads(response.read(100_000))
    except (OSError, ValueError, KeyError):
        return None
    return data if isinstance(data, dict) else None


def matches(cfg, data):
    return (isinstance(data, dict) and data.get('ok') is True and data.get('model') == cfg['model']
            and data.get('deployment_version') == cfg['deployment_version']
            and (not cfg.get('device') or str(data.get('device', '')).split(':')[0] == cfg['device']))


def probe(cfg, timeout=3):
    return matches(cfg, health_response(cfg, timeout))


def start(timeout=180):
    cfg = installed_config()
    preflight(cfg)
    is_loaded = loaded()
    status = health_response(cfg)
    if matches(cfg, status):
        if is_loaded:
            return 'already healthy'
        raise RuntimeError('a compatible sidecar already occupies the port outside this service')
    if status is not None:
        raise RuntimeError('port %d serves a different model, deployment version or device' % cfg['port'])
    if is_loaded:
        launch('kickstart', '-k', target())
    else:
        launch('bootstrap', 'gui/%d' % os.getuid(), str(PLIST))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe(cfg):
            return 'healthy on 127.0.0.1:%d' % cfg['port']
        time.sleep(2)
    # A broken cache or package should not respawn indefinitely under KeepAlive.
    launch('bootout', target())
    raise RuntimeError('Laya did not become healthy; service stopped. Check %s' % LOG)


def stop():
    owned_spec()  # also permits the old two-argument plist, but only for this checkout
    if not loaded():
        return 'already stopped'
    launch('bootout', target())
    return 'stopped'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    add = commands.add_parser('install', help='write the LaunchAgent plist without starting the model')
    add.add_argument('--replace', action='store_true', help='replace an existing, stopped Laya plist')
    add = commands.add_parser('start', help='launch and wait for matching model health')
    add.add_argument('--timeout', type=int, default=180, help='health deadline in seconds (default 180)')
    commands.add_parser('health', help='check the installed service model and deployment version')
    commands.add_parser('stop', help='stop this LaunchAgent')
    args = parser.parse_args(argv)
    try:
        if args.command == 'install':
            message = install(args.replace)
        elif args.command == 'start':
            if args.timeout <= 0:
                raise ValueError('--timeout must be positive')
            message = start(args.timeout)
        elif args.command == 'stop':
            message = stop()
        else:
            cfg = installed_config()
            if not probe(cfg):
                raise RuntimeError('Laya is not healthy on 127.0.0.1:%d' % cfg['port'])
            message = 'healthy on 127.0.0.1:%d (%s)' % (cfg['port'], cfg['model'])
        print(message)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
