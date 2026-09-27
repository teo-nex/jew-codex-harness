"""Independent native Codex evidence and scoped picker-default restoration."""
import json
from pathlib import Path
import re
import sqlite3
import os
import tomllib
from contextlib import closing


def defaults_snapshot(path=None):
    path = Path(path or Path.home() / '.codex/config.toml')
    data = tomllib.loads(path.read_text())
    return {k: {'present': k in data, 'value': data.get(k)} for k in ('model', 'model_reasoning_effort')}


def restore_picker_defaults(snapshot, path=None, *, selected_model='gpt-6-luna', selected_effort='medium'):
    path = Path(path or Path.home() / '.codex/config.toml')
    raw = path.read_text()
    data = tomllib.loads(raw)
    if data.get('model') != selected_model or data.get('model_reasoning_effort') != selected_effort:
        return {'restored': False, 'reason': 'defaults_changed_by_another_actor'}
    first_table = re.search(r'(?m)^\[', raw)
    end = first_table.start() if first_table else len(raw)
    head, tail = raw[:end], raw[end:]
    for key, previous in snapshot.items():
        pattern = r'(?m)^' + re.escape(key) + r'\s*=.*(?:\n|$)'
        replacement = key + ' = ' + json.dumps(previous['value']) + '\n' if previous['present'] else ''
        if re.search(pattern, head):
            head = re.sub(pattern, lambda _: replacement, head, count=1)
        elif replacement:
            head = replacement + head
    updated = head + tail
    expected = dict(data)
    for key, old in snapshot.items():
        if old['present']:
            expected[key] = old['value']
        else:
            expected.pop(key, None)
    if tomllib.loads(updated) != expected:
        raise ValueError('Unexpected default restoration diff')
    if path.read_text() != raw:
        return {'restored': False, 'reason': 'concurrent_config_write'}
    temp = path.with_name(path.name + '.jev-picker-' + str(os.getpid()))
    with temp.open('x') as stream:
        os.chmod(temp, path.stat().st_mode & 0o777)
        stream.write(updated)
    os.replace(temp, path)
    return {'restored': True}


def read_native_evidence(binding, codex_home=None, *, expected_model='gpt-6-luna', expected_effort='medium'):
    home = Path(codex_home or Path.home() / '.codex')
    db = home / 'state_5.sqlite'
    with closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True)) as conn:
        rows = conn.execute('select id, rollout_path, model_provider from threads where cwd=? and created_at_ms>=?',
                            (binding['cwd'], binding['started_at'] - 3000)).fetchall()
    # No guess when multiple concurrent threads share the launch directory.
    if len(rows) != 1:
        return {'verified': False, 'reason': 'native_thread_ambiguous_or_missing'}
    thread_id, rollout, provider = rows[0]
    path = Path(rollout).resolve()
    if not any(path.is_relative_to((home / name).resolve()) for name in ('sessions', 'archived_sessions')):
        return {'verified': False, 'reason': 'unexpected_rollout_path'}
    metadata = None
    context = None
    usage = None
    with path.open() as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            payload = item.get('payload', {})
            if item.get('type') == 'session_meta':
                metadata = payload
            elif item.get('type') == 'turn_context':
                context = payload
            elif payload.get('type') == 'token_count' and payload.get('info'):
                usage = payload['info'].get('total_token_usage') or usage
    if not metadata or metadata.get('id') != thread_id or metadata.get('cwd') != binding['cwd']:
        return {'verified': False, 'reason': 'native_metadata_mismatch'}
    if not context:
        return {'verified': False, 'reason': 'native_context_missing'}
    selected = {k: context.get(k) for k in ('model', 'effort', 'approval_policy', 'sandbox_policy', 'cwd')}
    good = (selected['model'] == expected_model
            and (expected_effort is None or selected['effort'] == expected_effort)
            and provider == 'openai'
            and selected['approval_policy'] == 'never'
            and isinstance(selected['sandbox_policy'], dict)
            and selected['sandbox_policy'].get('type') == 'danger-full-access')
    return {'verified': good, 'thread_id': thread_id, 'model_provider': provider,
            'context': selected, 'usage': usage, 'source': str(path),
            'reason': None if good else 'native_model_or_provider_mismatch'}
