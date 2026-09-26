"""Dataset writers using Apify field names. CSV flattens nested fields Apify-style ("latestPosts/0/url")."""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Callable, Iterable, TextIO


def omit_fields(items: list[dict], omit: Iterable[str] | None) -> list[dict]:
    omit = [o for o in (omit or []) if o]
    if not omit:
        return items
    return [{k: v for k, v in it.items() if k not in omit} for it in items]


def _put(out: dict, key: str, value: Any) -> None:
    if key in out:
        raise ValueError(f'CSV flattened field collision: {key!r}')
    out[key] = value


def _flatten(obj: Any, prefix: str, out: dict) -> None:
    if isinstance(obj, dict):
        if not obj and prefix:
            _put(out, prefix, "")
        for k, v in obj.items():
            _flatten(v, f"{prefix}/{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        if not obj and prefix:
            _put(out, prefix, "")
        for i, v in enumerate(obj):
            _flatten(v, f"{prefix}/{i}", out)
    elif isinstance(obj, bool):
        _put(out, prefix, "true" if obj else "false")      # JSON/Apify style, not Python True/False
    else:
        _put(out, prefix, "" if obj is None else obj)


def _csv_cell(value: Any) -> Any:
    """Keep untrusted profile text literal when a CSV is opened in a spreadsheet."""
    if isinstance(value, str) and value.lstrip("\ufeff \t\r\n\v\f").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_stream(items_factory: Callable[[], Iterable[dict]], stream: TextIO, fmt: str) -> int:
    """Write without retaining profile rows; return the number of rows written.

    CSV discovers columns in a first pass. The caller must supply a fresh,
    consistent iterator on each call, such as a held SQLite read transaction.
    JSON and JSONL consume the factory once. Text streams must use UTF-8 and
    newline='' when opened as files; callers own atomic file publication.
    """
    count = 0
    if fmt in ('json', 'jsonl'):
        if fmt == 'json':
            stream.write('[')
        for item in items_factory():
            if fmt == 'json':
                stream.write(',\n' if count else '\n')
            json.dump(item, stream, ensure_ascii=False, allow_nan=False)
            if fmt == 'jsonl':
                stream.write('\n')
            count += 1
        if fmt == 'json':
            stream.write('\n]\n' if count else ']\n')
        return count
    if fmt != 'csv':
        raise ValueError(f'Unsupported output format: {fmt}')
    columns, seen = [], set()
    for item in items_factory():
        flat = {}
        _flatten(item, '', flat)
        for key in flat:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='raise')
    writer.writerow({key: _csv_cell(key) for key in columns})
    for item in items_factory():
        flat = {}
        _flatten(item, '', flat)
        writer.writerow({key: _csv_cell(value) for key, value in flat.items()})
        count += 1
    return count


def _render_stream(items: list[dict], fmt: str) -> str:
    buffer = io.StringIO(newline='')
    write_stream(lambda: iter(items), buffer, fmt)
    return buffer.getvalue()


def to_csv(items: list[dict]) -> str:
    return _render_stream(items, 'csv')


def to_json(items: list[dict]) -> str:
    return _render_stream(items, 'json')


def to_jsonl(items: list[dict]) -> str:
    return _render_stream(items, 'jsonl')


def infer_format(path: str | None, fmt: str | None) -> str:
    if fmt:
        return fmt
    if path:
        for ext in ("jsonl", "csv", "json"):
            if path.lower().endswith("." + ext):
                return ext
    return "json"


def render(items: list[dict], fmt: str) -> str:
    return {"json": to_json, "jsonl": to_jsonl, "csv": to_csv}[fmt](items)
