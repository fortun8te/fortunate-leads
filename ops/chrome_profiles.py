"""Resolve only the explicitly configured Chrome profiles used by start-all."""
import json
import re
import sys
from pathlib import Path

CONFIG = Path(__file__).with_name('startup_profiles.json')


def configured_names(config_path=CONFIG):
    try:
        config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Chrome startup profile configuration could not be read: ' + str(exc)) from exc
    names = config.get('chrome_profiles') if isinstance(config, dict) else None
    if (not isinstance(names, list) or not names or
            any(not isinstance(name, str) or not name.strip() for name in names) or
            len(set(names)) != len(names)):
        raise ValueError('Chrome startup profiles must be a non-empty list of unique names')
    return names


def configured_directories(config_path=CONFIG):
    """Use preverified profile directories when the background service lacks Chrome registry access."""
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    names = configured_names(config_path)
    directories = config.get('chrome_profile_dirs')
    if (not isinstance(directories, list) or len(directories) != len(names) or
            any(not isinstance(directory, str) or not re.fullmatch(r'(?:Default|Profile [1-9][0-9]*)', directory)
                for directory in directories) or len(set(directories)) != len(directories)):
        raise ValueError('Configured Chrome profile directories must uniquely match the named accounts')
    return directories


def resolve_profiles(chrome_state, config_path=CONFIG):
    try:
        state = json.loads(Path(chrome_state).read_text(encoding='utf-8'))
        cache = state['profile']['info_cache']
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Chrome profiles could not be found: ' + str(exc)) from exc
    resolved = []
    for name in configured_names(config_path):
        found = [directory for directory, info in cache.items()
                 if isinstance(info, dict) and info.get('name') == name]
        if len(found) != 1:
            raise ValueError('Expected exactly one Chrome profile named ' + name)
        resolved.append(found[0])
    if len(set(resolved)) != len(resolved):
        raise ValueError('Configured Chrome profiles resolve to duplicate directories')
    return resolved


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args == ['--configured']:
        try:
            print('\n'.join(configured_directories()))
        except (OSError, ValueError, TypeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if len(args) != 1:
        print('Usage: chrome_profiles.py CHROME_LOCAL_STATE', file=sys.stderr)
        return 2
    try:
        print('\n'.join(resolve_profiles(args[0])))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
