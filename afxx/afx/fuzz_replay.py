"""AFX 0.4.1 replay and exact-result tracking; no shared database archive."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing

from afxx import common, config
from afxx.afx import replay


def validate_options(c, request):
    common.require_code(request.get('upload') in (None, True, False), 'INVALID_ARGUMENT',
                        'upload must be a boolean')


def command(interpreter, seed, device=None, compile_threads=None, dump=False):
    argv = [interpreter, '-m', 'afx', 'fuzz', 'replay', seed, '--save']
    for key, value in [('device', device), ('compile-threads', compile_threads)]:
        if value is not None:
            argv += ['--' + key, str(value)]
    return argv + (['--dump'] if dump else [])


def objects(text):
    decoder, values, offset = json.JSONDecoder(), [], 0
    while offset < len(text):
        if text[offset] == '{' and (offset == 0 or text[offset-1] == '\n'):
            try:
                value, size = decoder.raw_decode(text[offset:])
                values.append(value); offset += size
                continue
            except ValueError:
                pass
        end = text.find('\n', offset)
        offset = len(text) if end < 0 else end + 1
    return values


def public(args):
    result = subprocess.run([sys.executable, '-m', 'afx', *args], capture_output=True, text=True, timeout=60)
    values = objects(result.stdout)
    common.require_code(bool(values), 'AFX_PROTOCOL_ERROR', 'AFX returned no JSON: ' + ' '.join(args[:2]))
    value = values[-1]
    if value.get('ok') is False:
        error = value.get('error') or {}
        raise common.WorkflowError(error.get('code', 'AFX_COMMAND_FAILED'), error.get('message', 'AFX command failed'))
    common.require_code(result.returncode == 0, 'AFX_COMMAND_FAILED', 'AFX command failed: ' + ' '.join(args[:2]))
    return value.get('data', value)


def invoke(argv, timeout, log):
    with log.open('w') as out, subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, start_new_session=True) as process:
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM); process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
            except ProcessLookupError:
                process.wait()
            raise
    return code, objects(log.read_text(errors='replace'))


def task_rows(queue, ids, home):
    # No public CLI currently maps task IDs to attempts. Keep this fallback read-only.
    common.require_code(queue.is_absolute() and home.resolve() in queue.resolve().parents
                        and queue.name == 'tasks.sqlite'
                        and not any(p.is_symlink() for p in (queue, *queue.parents)),
                        'AFX_PROTOCOL_ERROR', 'invalid AFX queue path')
    with closing(sqlite3.connect(queue.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for identity in ids for row in db.execute(
            'SELECT id,state,attempt_id,seed,execution FROM tasks WHERE id=?', (identity,))]
    common.require_code(bool(ids) and len(rows) == len(set(ids)) == len(ids), 'AFX_PROTOCOL_ERROR', 'missing or duplicate replay tasks')
    return rows


def resolve_receipt(receipt, home, database, deadline, checkpoint):
    if 'task_ids' not in receipt:
        ids = [a['attempt_id'] for a in receipt.get('attempts', [])]
        common.require_code(bool(ids) and len(ids) == len(set(ids)), 'AFX_PROTOCOL_ERROR', 'missing replay attempt IDs')
        return database, ids
    queue = Path(receipt['queue'])
    while True:
        rows = task_rows(queue, receipt['task_ids'], home)
        checkpoint(rows)
        if any(row['state'] in ('cancelled', 'interrupted', 'failed') for row in rows):
            raise common.WorkflowError('AFX_REPLAY_FAILED', 'replay task cancelled or interrupted')
        if all(row['state'] == 'completed' and row['attempt_id'] for row in rows):
            return (queue.parent / 'results.sqlite' if queue.parent.parent.name == 'runs' else database,
                    [row['attempt_id'] for row in rows])
        common.require_code(time.monotonic() < deadline, 'REPLAY_WAIT_TIMEOUT',
                            'tasks are still running; receipt preserved; do not resubmit')
        time.sleep(2)


def read_attempt(database, identity):
    attempt = public(['results', 'list', str(database), '--attempt', identity])
    common.require_code(attempt.get('id') == identity, 'AFX_PROTOCOL_ERROR', 'attempt identity mismatch')
    return attempt


def upload_results(c, attempts):
    """Sync ONLY selected attempts via a disposable staging DB, never the source DB.

    The public sync CLI is database-scoped. AFX's repository writer preserves
    normalized content and UUIDs; validate hashes before any network call.
    """
    from autofusex.execution.storage import ResultRepository
    settings = config.platform_settings(c)
    with tempfile.TemporaryDirectory(prefix='afxx-upload-') as directory:
        database = Path(directory) / 'results.sqlite'
        repository = ResultRepository(database)
        for attempt in attempts:
            contexts = attempt['contexts']
            value = dict(attempt, execution=contexts['execution'], evaluation=contexts['evaluation'])
            repository.save(value, environment=contexts['environment'], attempt_id=attempt['id'])
            saved = repository.detail(attempt['id'])
            common.require_code(saved['result_hash'] == attempt['result_hash'], 'UPLOAD_CONTENT_MISMATCH',
                                'AFX normalized upload content differs from original attempt')
            with sqlite3.connect(database) as db:
                db.execute('UPDATE attempts SET created_at=? WHERE id=?', (attempt['created_at'], attempt['id']))
        common.require_code(bool(attempts), 'UPLOAD_EMPTY', 'no selected results to upload')
        receipt = public(['sync', 'once', '--endpoint', settings['url'], '--token-file',
                          (settings.get('client_token_file') or settings['remote_token_file']), '--results', str(database),
                          '--state', str(Path(directory) / 'sync.sqlite')])
        # The public receipt counts attempted uploads, including rejects.
        with closing(sqlite3.connect((Path(directory) / 'sync.sqlite').as_uri() + '?mode=ro', uri=True)) as state:
            rejected = state.execute('SELECT attempt_id,reason FROM rejected').fetchall()
        common.require_code(not rejected and receipt.get('uploaded') == len(attempts),
                            'REPLAY_UPLOAD_FAILED', 'AFX did not accept all selected attempts: ' + str(rejected))
    return {'scope': 'selected_attempts_only', 'attempt_ids': [a['id'] for a in attempts], 'receipt': receipt}


def dump_directories(home):
    # Workers may be reused: observe file changes, not just new directories.
    roots = [home / 'debug', *home.glob('runs/*/debug')]
    observed = {}
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        for worker in root.glob('**/worker-*'):
            if not worker.is_dir() or any(p.is_symlink() for p in (worker, *worker.parents)):
                continue
            count, newest = 0, 0
            for directory, dirs, files in os.walk(worker, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
                for name in files:
                    path = Path(directory) / name
                    try:
                        if path.is_symlink() or not path.is_file():
                            continue
                        newest = max(newest, path.stat().st_mtime_ns)
                        count += 1
                    except FileNotFoundError:
                        continue  # AFX cleanup may run concurrently.
            observed[str(worker.resolve())] = (count, newest)
    return observed


def execute(job):
    from autofusex.client.paths import data_home, results_database
    job = Path(job)
    c, request = common.read(job / 'config.yaml'), common.read(job / 'request.json')
    from afxx.afx.managed_client import selected_client
    selected = selected_client()
    home = Path(selected['home']) if selected else data_home()
    # Route through the unique live client even if active-home points elsewhere.
    os.environ['AUTOFUSEX_HOME'] = str(home)
    enabled = c['upload']['enabled'] if request.get('upload') is None else request['upload']
    active = []
    if selected:
        state = public(['daemon', 'status', '--data-dir', str(home)])
        active = [run for run in state.get('runs', []) if run['state'] in
                  ('running', 'preparing', 'paused', 'pausing', 'draining', 'needs_attention')]
        for run in active:
            spec = public(['task', 'inspect', run['id'], '--data-dir', str(home)]).get('spec', {})
            spec = spec.get('spec', spec)
            common.require_code(spec.get('results', {}).get('upload', True) == enabled,
                                'UPLOAD_POLICY_CONFLICT',
                                'active AFX session upload policy differs from replay; change the session policy first')
    if enabled and not active:
        settings = config.platform_settings(c)
        common.require_code(Path(settings.get('client_token_file') or settings['remote_token_file']).is_file(),
                            'PLATFORM_CREDENTIAL_MISSING',
                            'standalone upload requires platform.client_token_file; use --no-upload for offline replay')
    results = Path(c['replay']['results_dir']) / job.name
    results.mkdir(parents=True, exist_ok=True)
    dump = c['replay']['dump']
    before = dump_directories(home) if dump else {}
    common.update_record(job / 'result.json', execution_mode='fuzz_replay', client_home=str(home),
        results_dir=str(results), upload={'enabled': enabled, 'state': 'awaiting_execution' if enabled else 'disabled'},
        references=[], attempts=[], dump_enabled=dump)
    normalized = replay.normalized_seeds(common.capture(replay.seed_command(sys.executable, request['seeds'])))
    references, attempts, errors = [], [], []
    standalone_attempts, client_attempt_ids = [], []
    try:
        for index, seed in enumerate(request['seeds']):
            devices = c['replay']['devices']
            device = None if devices == 'auto' else devices[index % len(devices)]
            argv = command(sys.executable, seed, device, c['replay']['compile_threads'], dump)
            log = job / ('replay-' + str(index + 1) + '.log')
            entry = {'seed': seed, 'command': argv, 'log': str(log)}
            references.append(entry)
            common.phase(job, 'fuzz_replay', seed=seed)
            common.update_record(job / 'result.json', references=references)
            deadline = time.monotonic() + c['replay']['batch_timeout_seconds']
            try:
                code, values = invoke(argv, c['replay']['batch_timeout_seconds'], log)
            except subprocess.TimeoutExpired:
                entry['state'] = 'submission_unknown'
                common.update_record(job / 'result.json', references=references)
                raise common.WorkflowError('REPLAY_WAIT_TIMEOUT', 'CLI timed out; inspect its log before retrying') from None
            entry['exit_code'] = code
            receipt = next((v for v in reversed(values) if 'task_ids' in v or 'attempts' in v), None)
            common.require_code(receipt is not None, 'AFX_PROTOCOL_ERROR', 'no task/attempt receipt; inspect ' + str(log))
            entry['receipt'] = receipt
            common.update_record(job / 'result.json', references=references)
            def checkpoint(rows):
                entry['tasks'] = rows
                common.update_record(job / 'result.json', references=references)
            database, identities = resolve_receipt(receipt, home, results_database(home), deadline, checkpoint)
            entry.update(database=str(database), attempt_ids=identities)
            current = [read_attempt(database, identity) for identity in identities]
            attempts.extend(current)
            entry['upload_owner'] = 'client' if 'task_ids' in receipt else 'afxx'
            if entry['upload_owner'] == 'client':
                client_attempt_ids.extend(identities)
            else:
                standalone_attempts.extend(current)
            if enabled:
                common.update_record(job / 'result.json', upload={
                    'enabled': True, 'owner': 'client' if not standalone_attempts else 'afxx' if not client_attempt_ids else 'mixed',
                    'state': 'delegated' if not standalone_attempts else 'pending',
                    'client_attempt_ids': client_attempt_ids, 'server_confirmation': 'not_checked'})
            if code != 0 and 'task_ids' not in receipt:
                errors.append('AFX replay failed: ' + seed)
            common.update_record(job / 'result.json', references=references, attempts=attempts)
        report = replay.summarize(attempts, normalized)
        report['errors'].extend(errors)
        report['passed'] = not report['errors']
        report['execution_environments'] = {a['id']: a.get('contexts', {}).get('environment', {}) for a in attempts}
        common.update_record(job / 'result.json', **report)
        if enabled and standalone_attempts:
            common.phase(job, 'replay_upload')
            try:
                receipt = upload_results(c, standalone_attempts)
            except Exception:
                common.update_record(job / 'result.json', upload={'enabled': True, 'state': 'failed'})
                raise
            common.update_record(job / 'result.json', upload={'enabled': True, 'owner': 'mixed' if client_attempt_ids else 'afxx',
                'state': 'delegated' if client_attempt_ids else 'synced',
                'client_attempt_ids': client_attempt_ids, **receipt})
        common.require_code(report['passed'], 'REPLAY_FAILED', 'fuzz replay failed; see result.json')
    finally:
        if dump:
            common.update_record(job / 'result.json', dump_roots=sorted(path for path, stamp in dump_directories(home).items()
                                  if before.get(path) != stamp),
                dump_association='candidate_worker_directories; shared workers may contain other tasks')


def main():
    try:
        from importlib.metadata import version
        from packaging.version import Version
        installed = version('autofusex')
        common.require_code(Version(installed) >= Version('0.4.1'), 'AFX_VERSION_UNSUPPORTED',
                            'replay requires AFX >= 0.4.1; installed: ' + installed)
        execute(Path(sys.argv[1]))
    except Exception as error:
        common.update_record(Path(sys.argv[1]) / 'result.json', passed=False,
                             error_code=getattr(error, 'code', 'AFX_REPLAY_FAILED'), error=str(error))
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
