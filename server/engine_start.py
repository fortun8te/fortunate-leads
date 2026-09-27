"""Run the existing, ownership-checked one-click service launcher from the UI."""
import re
import json
import subprocess
from pathlib import Path


class EngineStartError(RuntimeError):
    pass


def configured_profile_count(repo):
    config_path = Path(repo) / 'ops' / 'startup_profiles.json'
    try:
        names = json.loads(config_path.read_text(encoding='utf-8')).get('chrome_profiles')
    except (OSError, ValueError, AttributeError) as exc:
        raise EngineStartError('The configured Chrome profiles could not be read.') from exc
    if (not isinstance(names, list) or not names or
            any(not isinstance(name, str) or not name.strip() for name in names) or
            len(set(names)) != len(names)):
        raise EngineStartError('Chrome startup profiles must be a non-empty list of unique names.')
    return len(names)


def start(repo, account_count, run=subprocess.run):
    repo = Path(repo).resolve()
    launcher = repo / 'ops' / 'start-all.command'
    if not launcher.is_file() or not launcher.stat().st_mode & 0o111:
        raise EngineStartError('The configured Start engine launcher is missing or not executable.')
    try:
        result = run([str(launcher), '--from-app'], cwd=str(repo), capture_output=True,
                     text=True, timeout=210, check=False)
    except subprocess.TimeoutExpired as exc:
        raise EngineStartError('Startup took too long. Check the Accounts page and Laya status before trying again.') from exc
    except OSError as exc:
        raise EngineStartError('Could not run the Start engine launcher: ' + str(exc)) from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or '').strip().splitlines()
        message = detail[-1] if detail else 'The startup launcher failed.'
        raise EngineStartError(message[:300])
    match = re.search(r'^ENGINE_STARTED:(\d+)\s*$', result.stdout or '', re.MULTILINE)
    if not match:
        raise EngineStartError('The launcher finished without confirming engine startup.')
    opened = int(match.group(1))
    if opened != 0:
        raise EngineStartError('Local startup unexpectedly opened Instagram profiles.')
    return {'ok': True, 'local_services_started': True, 'profiles_opened': 0}
