"""Portable AFX lifecycle adapter, executed in the remote Python environment."""
import contextlib
import json
import os
from pathlib import Path
import sys
import time


class ClientError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def alive(pid):
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, ValueError, IndexError):
        return False


def live_clients():
    """Discover current-user resident clients; a supervisor and its child count once."""
    found = []
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            if proc.stat().st_uid != os.getuid() or not alive(proc.name):
                continue
            argv = (proc / 'cmdline').read_bytes().decode().split('\0')
            if '-m' in argv:
                if argv[argv.index('-m') + 1] not in ('afx', 'autofusex'):
                    continue
            elif not any(Path(value).name in ('afx', 'autofusex') for value in argv[:2]):
                continue
            legacy = 'platform' in argv and 'client' in argv and 'run' in argv
            daemon = 'daemon' in argv and ('run' in argv or 'supervise' in argv)
            if not (legacy or daemon):
                continue
            directory = argv[argv.index('--data-dir') + 1] if '--data-dir' in argv else None
            if not directory:
                env = dict(part.split('=', 1) for part in (proc/'environ').read_text().split('\0') if '=' in part)
                directory = env.get('AUTOFUSEX_HOME')
            parent = int((proc/'stat').read_text().rsplit(')', 1)[1].split()[1])
            found.append({'pid': int(proc.name), 'parent': parent,
                          'home': str(Path(directory).resolve()) if directory else None,
                          'legacy': legacy, 'supervisor': daemon and 'supervise' in argv})
        except (OSError, ValueError, IndexError):
            continue
    return [item for item in found if not (item['supervisor'] and any(
        child['parent'] == item['pid'] and not child['legacy'] and not child['supervisor']
        and child['home'] == item['home'] for child in found))]


def selected_client():
    clients = live_clients()
    if len(clients) > 1:
        raise ClientError('PLATFORM_CLIENT_CONFLICT', 'multiple live AFX clients: ' + json.dumps(clients))
    if clients and (clients[0]['legacy'] or not clients[0]['home']):
        raise ClientError('PLATFORM_CLIENT_UNTRACKED', 'live client is legacy or its home cannot be determined')
    return clients[0] if clients else None


def execute(settings, run, legacy):
    home = Path(settings['home'])
    action = settings.get('action', 'start')
    timeout = settings.get('timeout', 300)
    deadline = time.monotonic() + timeout
    prefix = ['--data-dir', str(home)]
    selected = selected_client()
    if selected and Path(selected['home']).resolve() != home.resolve():
        raise ClientError('PLATFORM_CLIENT_CONFLICT', 'live client changed during lifecycle operation; query status again')
    conflicts = []

    def status():
        try:
            # The daemon endpoint does not collect/import AF or require CANN setup.
            return legacy(['daemon', 'status', '--data-dir', str(home)])
        except (FileNotFoundError, ConnectionRefusedError):
            return None

    def wait():
        if time.monotonic() >= deadline:
            raise ClientError('PLATFORM_CLIENT_TIMEOUT', 'client operation timed out; query client status; no forced termination performed')
        time.sleep(1)

    def view(value):
        initialized = (home/'client.json').exists()
        runs = value.get('runs', []) if value else []
        active = [r for r in runs if r['state'] in ('preparing','running','paused','pausing','draining','cancelling','needs_attention')]
        state = 'uninitialized' if not initialized and not value else 'stopped' if not value else 'idle'
        if active:
            state = 'draining' if any(r['state'] in ('draining','cancelling') for r in active) else 'running' if any(r['state']=='running' for r in active) else active[0]['state']
        identity = json.loads((home/'config.json').read_text()).get('source_id') if (home/'config.json').exists() else None
        return dict(state=state, daemon_running=bool(value), machine_id=identity, runs=runs,
                    active_operation=value.get('active_operation') if value else None,
                    pid=value.get('pid') if value else None, conflicts=conflicts,
                    retest_readiness='not_checked')

    current = status()
    if action == 'status':
        return view(current)

    if action == 'restart' and not current:
        raise ClientError('PLATFORM_CLIENT_NOT_RUNNING', 'no live client to restart; use client start')

    # Existing client configuration belongs to AFX; workflow settings only seed a new home.
    if action == 'start' and not (home/'client.json').exists():
        token = Path(settings['token_file']).read_text().strip() if settings.get('token_file') else None
        if not token or any(c.isspace() for c in token):
            raise ClientError('CONFIG_INVALID', 'fill platform.client_token_file to initialize AFX client')
        if (home/'platform-client.json').exists() and not (home/'config.json').exists():
            raise ClientError('PLATFORM_CLIENT_MIGRATION_REQUIRED', 'legacy client identity requires explicit migration before managed init')
        argv = prefix + ['init','--endpoint',settings['endpoint'],'--token',token]
        for device in settings['devices']:
            argv += ['--device',device]
        run(argv)

    if action in ('stop', 'restart') and current:
        daemon_pid = current.get('pid')
        supervisor_pid = None
        try:
            parent = Path(f'/proc/{daemon_pid}/stat').read_text().rsplit(')', 1)[1].split()[1]
            if b'daemon\x00supervise' in Path(f'/proc/{parent}/cmdline').read_bytes():
                supervisor_pid = int(parent)
        except OSError:
            pass
        run(prefix + ['stop'])
        while True:
            current = status()
            if current is None:
                break
            active = view(current)['state'] not in ('idle','stopped','uninitialized')
            if not active and not current.get('active_operation'):
                try:
                    legacy(['daemon','stop','--data-dir',str(home)])
                    break
                except ValueError:
                    # AFX may have observed worker completion before releasing process ownership.
                    pass
            wait()
        while status() is not None or (daemon_pid and alive(daemon_pid)) or (supervisor_pid and alive(supervisor_pid)):
            wait()
    if action == 'stop':
        return view(None)

    session = run(prefix + ['start'])
    ids = session.get('run_ids') or [session.get('run_id')]
    while True:
        current = status()
        states = {r['id']:r['state'] for r in current.get('runs', [])} if current else {}
        if ids and all(states.get(i) == 'running' for i in ids):
            return dict(view(current), session=session, status=current)
        if not current or any(states.get(i) not in ('preparing','running') for i in ids):
            raise ClientError('PLATFORM_CLIENT_START_FAILED', 'AFX execution session did not become running')
        wait()


def main():
    settings = json.loads(sys.argv[1])
    try:
        from autofusex.fuzz.daemon.management.modern_cli import run, _legacy
        with contextlib.redirect_stdout(sys.stderr):
            result = execute(settings, run, _legacy)
    except ClientError as error:
        result = {'error': error.code, 'message': str(error)}
    except Exception as error:
        # Do not forward upstream exceptions that may include credentials.
        print('AFX client failed: ' + type(error).__name__, file=sys.stderr)
        result = {'error': 'PLATFORM_CLIENT_CONTROL_FAILED', 'message': 'AFX lifecycle operation failed; inspect client.log'}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
