"""Managed replay submission and exact-task result tracking.

AFX owns execution. The only SQLite fallback is a read-only task ledger query;
its CLI currently exposes attempts but not lookup by the returned task IDs.
"""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from contextlib import closing

from afxx import common
from afxx.afx import replay


FAILED = {'failed', 'cancelled', 'interrupted', 'stopped', 'needs_attention'}


def validate_options(c, request):
    common.require_code(not c['replay']['dump'], 'MANAGED_REPLAY_UNSUPPORTED',
                        'afx replay 尚不支持逐任务 dump；不会回退到 fuzz replay')
    common.require_code(request.get('upload') is None and not c['upload']['enabled'],
                        'MANAGED_REPLAY_UNSUPPORTED',
                        'managed replay 的上传由 AFX client 管理，不接受 AFXX upload 覆盖')


def command(interpreter, seeds, home):
    return [interpreter, '-m', 'afx', 'replay', *seeds, '--data-dir', str(home)]


def invoke(argv):
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, check=False, timeout=60)
    try:
        body = json.loads(result.stdout)
        common.require_code(isinstance(body, dict) and type(body.get('ok')) is bool,
                            'AFX_PROTOCOL_ERROR', 'invalid AFX response')
    except (ValueError, TypeError) as error:
        raise common.WorkflowError('AFX_PROTOCOL_ERROR', 'AFX did not return a JSON envelope; submission may be uncertain') from error
    if not body['ok']:
        error = body.get('error') or {}
        raise common.WorkflowError(error.get('code', 'AFX_REPLAY_FAILED'), error.get('message', 'AFX request failed'))
    # Inspection can exit nonzero for a failed operation while returning ok=true.
    common.require_code(isinstance(body.get('data'), dict), 'AFX_PROTOCOL_ERROR', 'missing AFX data')
    return body['data']


def task_rows(home, run_id, ids):
    common.validate_job_id(run_id)
    path = Path(home) / 'runs' / run_id / 'tasks.sqlite'
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if ids is None:
            rows = db.execute('SELECT id,seed,state,attempt_id,execution,reason FROM tasks').fetchall()
        else:
            rows = []
            for identity in ids:
                rows.extend(db.execute('SELECT id,seed,state,attempt_id,execution,reason FROM tasks WHERE id=?', (identity,)).fetchall())
        return [dict(row) for row in rows]


def track(receipt, query, read_tasks, checkpoint, sleep=time.sleep):
    """Wait for this submission, never for an unrelated shared session to end."""
    operations = receipt.get('operation_ids')
    run_id, task_ids = receipt.get('run_id'), receipt.get('task_ids')
    common.require_code(
        (isinstance(operations, list) and bool(operations) and not run_id)
        or (isinstance(run_id, str) and isinstance(task_ids, list) and bool(task_ids) and not operations),
        'AFX_PROTOCOL_ERROR', 'unsupported managed replay receipt')
    owned = {run_id: task_ids} if run_id else {}
    pending = set(operations or [])
    done, attempts, tasks, environments = set(), [], [], {}
    while pending or set(owned) - done:
        for operation_id in list(pending):
            operation = query(['operation', 'inspect', operation_id])
            if operation['state'] in FAILED:
                raise common.WorkflowError('AFX_REPLAY_FAILED', 'AFX operation failed: ' + json.dumps(operation.get('result')))
            if operation['state'] == 'succeeded':
                identity = (operation.get('result') or {}).get('run_id')
                common.require_code(isinstance(identity, str), 'AFX_PROTOCOL_ERROR', 'operation has no run_id')
                common.validate_job_id(identity)
                owned[identity] = None  # newly created, dedicated replay run
                pending.remove(operation_id)
        for identity, selected in list(owned.items()):
            if identity in done:
                continue
            common.validate_job_id(identity)
            run = query(['task', 'inspect', identity])
            rows = read_tasks(identity, selected)
            if selected is not None:
                common.require_code({r['id'] for r in rows} == set(selected),
                                    'AFX_PROTOCOL_ERROR', 'returned replay tasks are missing from the ledger')
            if any(row['state'] in FAILED for row in rows):
                raise common.WorkflowError('AFX_REPLAY_FAILED', 'replay task interrupted or cancelled; inspect managed receipt')
            if rows and all(row['state'] == 'completed' for row in rows):
                for row in rows:
                    common.require_code(bool(row['attempt_id']), 'AFX_PROTOCOL_ERROR', 'completed task has no attempt_id')
                    detail = query(['task', 'results', identity, '--attempt', row['attempt_id']])
                    attempt = detail.get('attempt')
                    common.require_code(isinstance(attempt, dict) and attempt.get('id') == row['attempt_id'],
                                        'AFX_PROTOCOL_ERROR', 'task/attempt identity mismatch')
                    attempts.append(attempt)
                tasks.extend(rows)
                environments[identity] = run.get('spec', {})
                done.add(identity)
            elif run['state'] in FAILED or run['state'] == 'completed':
                raise common.WorkflowError('AFX_REPLAY_FAILED', 'managed run ended without all requested replay results')
        checkpoint({'receipt': receipt, 'run_ids': list(owned), 'completed_run_ids': sorted(done)}, attempts)
        if pending or set(owned) - done:
            sleep(2)
    return attempts, tasks, environments


def execute(job):
    from autofusex.fuzz.client.config import data_home
    job = Path(job)
    c, request = common.read(job / 'config.yaml'), common.read(job / 'request.json')
    validate_options(c, request)
    home = data_home()
    common.require_code((home / 'client.json').is_file(), 'PLATFORM_CLIENT_NOT_INITIALIZED',
                        '请先配置现有 AFX client；AFXX 不为 replay 创建新 client')
    interpreter = sys.executable
    normalized = replay.normalized_seeds(common.capture(replay.seed_command(interpreter, request['seeds'])))
    common.phase(job, 'managed_replay_submit', client_home=str(home))
    common.update_record(job / 'result.json', execution_mode='managed', client_home=str(home),
                         execution_settings_source='AFX client/history', upload='managed',
                         results_dir=str(Path(c['replay']['results_dir']) / job.name))
    # Never retry submission automatically: AFX replay has no caller idempotency key.
    receipt = invoke(command(interpreter, request['seeds'], home))
    common.update_record(job / 'result.json', managed={'receipt': receipt})
    common.phase(job, 'managed_replay_wait')

    def checkpoint(value, attempts):
        common.update_record(job / 'result.json', managed=value, attempts=attempts)

    attempts, tasks, specs = track(
        receipt,
        lambda args: invoke([interpreter, '-m', 'afx', *args, '--data-dir', str(home)]),
        lambda identity, ids: task_rows(home, identity, ids), checkpoint,
    )
    counts = dict(normalized)
    expected = {}
    for task in tasks:
        ex = json.loads(task['execution'])
        key = (task['seed'], ex['backend'])
        common.require_code(key not in expected and task['seed'] in counts,
                            'AFX_PROTOCOL_ERROR', 'unexpected or duplicate replay task')
        expected[key] = 1 if ex.get('static_only') else counts[task['seed']]
    report = replay.summarize(attempts, normalized, expected=expected)
    # A managed session's environment may differ from the submitting interpreter.
    report['execution_environments'] = {a['id']: a.get('contexts', {}).get('environment', {}) for a in attempts}
    report['managed_specs'] = specs
    common.update_record(job / 'result.json', **report)
    common.require_code(report['passed'], 'REPLAY_FAILED', 'managed replay failed; see result.json')


def main():
    job = Path(sys.argv[1])
    try:
        execute(job)
    except Exception as error:
        common.update_record(job / 'result.json', passed=False,
                             error_code=getattr(error, 'code', 'AFX_REPLAY_FAILED'), error=str(error))
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
