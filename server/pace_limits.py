"""Pace defaults, bounds and presets, read from the one file the extension also loads.

`extension/lib/pace-limits.js` is the single source. This module parses the strict-JSON block between its two marker
lines and mirrors the extension's clamp (lib/core.js clampPace) so both sides produce identical values and the same
`ident()` text. Nothing here is a second copy of a number.

Test-only override: FL_PACE_LIMITS_FILE=/path/to/another/pace-limits.js (the simulator uses it to show widened bounds).
"""
import json
import math
import os
import re
import sys
from pathlib import Path

PATH = Path(os.environ.get('FL_PACE_LIMITS_FILE') or
            Path(__file__).resolve().parent.parent / 'extension' / 'lib' / 'pace-limits.js')
_BLOCK = re.compile(r'//\s*BEGIN-PACE-LIMITS-JSON\s*const LIMITS = (\{.*?\});?\s*//\s*END-PACE-LIMITS-JSON', re.S)
ORDER = ('list_gap', 'profile_gap', 'spacing', 'break_every', 'break_len', 'window_max')
TAGS = {'list_gap': 'lg', 'profile_gap': 'pg', 'spacing': 'sp', 'break_every': 'be', 'break_len': 'bl', 'window_max': 'wm'}


def load(path=None):
    text = Path(path or PATH).read_text(encoding='utf-8')
    found = _BLOCK.search(text)
    if not found:
        raise RuntimeError('pace-limits.js: the JSON block markers are missing')
    limits = json.loads(found.group(1))
    check(limits)
    return limits


def check(limits):
    """The same self-consistency test lib/core.js runs at load: an impossible bound must fail loudly."""
    fields = limits.get('fields') or {}
    for f in ORDER:
        spec = fields.get(f)
        if spec and spec['kind'] == 'range':
            fl, ce, de = spec['floor'], spec['ceiling'], spec['default']
            ok = (fl[0] <= ce[0] and fl[1] <= ce[1] and max(fl[1], fl[0] * spec['min_spread']) <= ce[1]
                  and fl[0] <= de[0] <= ce[0] and fl[1] <= de[1] <= ce[1])
        else:
            ok = bool(spec) and spec['floor'] <= spec['default'] <= spec['ceiling']
        if not ok:
            raise RuntimeError(f'pace-limits.js: inconsistent bounds for {f}')
    if set(limits.get('budget_max') or {}) != {'list', 'profile'}:
        raise RuntimeError('pace-limits.js: budget_max needs list and profile')


LIMITS = load()
FIELDS = LIMITS['fields']


def budget_max():
    return dict(LIMITS['budget_max'])


def budget_default():
    return dict(LIMITS['budget_default'])


def defaults():
    return {f: json.loads(json.dumps(FIELDS[f]['default'])) for f in ORDER}


def _clip(x, lo, hi):
    return min(hi, max(lo, x))


def _round_to(x, step):
    return round(math.floor(x / step + 0.5) * step, 3)


def _ceil_to(x, step):
    return round(math.ceil(x / step - 1e-9) * step, 3)


def _floor_to(x, step):
    return round(math.floor(x / step + 1e-9) * step, 3)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _fit(spec, x, i=None):
    lo, hi = (spec['floor'], spec['ceiling']) if i is None else (spec['floor'][i], spec['ceiling'][i])
    return _clip(_round_to(_clip(x, lo, hi), spec['step']), lo, hi)


def clamp_range(spec, pair):
    lo, hi = (pair[0], pair[1]) if pair[0] <= pair[1] else (pair[1], pair[0])
    a, b = _fit(spec, lo, 0), _fit(spec, hi, 1)
    need = a * spec['min_spread']
    if b < need:
        b = min(spec['ceiling'][1], _ceil_to(need, spec['step']))
        if b < need:
            a = max(spec['floor'][0], _floor_to(b / spec['min_spread'], spec['step']))
    return [_clean(a), _clean(b)]


def _clean(x):
    """7.0 -> 7 so JSON and ident() text match what JavaScript prints."""
    return int(x) if float(x).is_integer() else x


def scale_default(f, centre):
    """A single number means: the default range, scaled so its centre is `centre`."""
    lo, hi = FIELDS[f]['default']
    factor = centre / ((lo + hi) / 2)
    return [lo * factor, hi * factor]


def _unit(f, n):
    unit = FIELDS[f]['unit']
    return f"{_clean(n)} {'s' if unit == 's' else unit}"


def explain(f, want, got):
    """Plain reason a requested value changed (for the API's `clamped` list)."""
    spec = FIELDS[f]
    unit = 's' if spec['unit'] == 's' else spec['unit']
    if spec['kind'] == 'int':
        n = want if want == want else 0
        return (f"The lowest allowed is {_clean(spec['floor'])} {unit}." if n < spec['floor'] else
                f"The highest allowed is {_clean(spec['ceiling'])} {unit}." if n > spec['ceiling'] else 'Rounded to a whole number.')
    fl, ce = spec['floor'], spec['ceiling']
    if min(want[0], want[1]) < fl[0] or max(want[0], want[1]) < fl[1]:
        return f"The lowest allowed is {_clean(fl[0])} to {_clean(fl[1])} {unit}."
    if min(want[0], want[1]) > ce[0] or max(want[0], want[1]) > ce[1]:
        return f"The highest allowed is {_clean(ce[0])} to {_clean(ce[1])} {unit}."
    if want[0] > want[1]:
        return 'The range was the wrong way round.'
    return f"The two numbers need to be at least {round((spec['min_spread'] - 1) * 100)}% apart."


def clamp(values, base=None, strict=False):
    """(values, notes): `values` is a full set inside the bounds. Fields missing from `values` keep `base`
    (default: the shipped defaults). With strict=True an unusable field raises ValueError instead of falling back."""
    base = base or defaults()
    out, notes = {}, []
    for f in ORDER:
        spec = FIELDS[f]
        want = values.get(f) if isinstance(values, dict) and f in values else base[f]
        explicit = isinstance(values, dict) and f in values
        if spec['kind'] == 'int':
            usable = _num(want)
            got = _clip(_round_to(_clip(want, spec['floor'], spec['ceiling']), spec['step']), spec['floor'], spec['ceiling']) if usable else base[f]
            got = _clean(got)
        else:
            if explicit and _num(want):   # one number: the default shape scaled to that centre
                want = [_round_to(x, spec['step']) for x in scale_default(f, want)]
            usable = isinstance(want, (list, tuple)) and len(want) == 2 and all(_num(x) for x in want)
            want = list(want) if usable else want
            got = clamp_range(spec, want) if usable else clamp_range(spec, base[f])
        if not usable:
            if strict and explicit:
                raise ValueError(f"{spec['label']} needs {'a whole number' if spec['kind'] == 'int' else 'a number or two numbers'}.")
            notes.append({'field': f, 'requested': want, 'applied': got, 'why': 'Not a usable value; kept the current one.'})
        elif got != want:
            notes.append({'field': f, 'requested': want, 'applied': got, 'why': explain(f, want, got)})
        out[f] = got
    return out, notes


def _fmt(x):
    return str(_clean(round(float(x), 3)))


def ident(values):
    """Same text as lib/core.js paceId(): how the server and an extension tell whether they agree."""
    parts = []
    for f in ORDER:
        v = values[f]
        parts.append(TAGS[f] + ('-'.join(_fmt(x) for x in v) if isinstance(v, (list, tuple)) else _fmt(v)))
    return '|'.join(parts)


def preset_ids():
    return list(LIMITS['presets'])


def preset(name):
    """(values, notes) a preset resolves to under the current bounds. Faster-than-allowed asks are clamped."""
    spec = LIMITS['presets'].get(name)
    if spec is None:
        raise ValueError('Choose slow, normal or fast.' if {'slow', 'normal', 'fast'} <= set(LIMITS['presets']) else 'Unknown preset.')
    return clamp(spec['values'])


if __name__ == '__main__':   # python3 server/pace_limits.py '{"list_gap":[1,2]}' -> clamped values and id (used by the parity test)
    got, notes = clamp(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {})
    print(json.dumps({'values': got, 'id': ident(got), 'notes': notes}))
