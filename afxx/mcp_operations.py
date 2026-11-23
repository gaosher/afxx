"""Durable receipts for detached CLI invocations, not a remote task scheduler."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

_CHILDREN = []


def root():
    return Path.home() / '.local/state/afxx/mcp-operations'


def write(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def error(code, message):
    return {'schema_version': 1, 'ok': False, 'command': 'mcp operation', 'data': None,
            'error': {'code': code, 'message': message}}


def status(request_id):
    _CHILDREN[:] = [child for child in _CHILDREN if child.poll() is None]
    identity = hashlib.sha256(request_id.encode()).hexdigest()
    directory = root() / identity
    if not (directory/'request.json').exists():
        return error('OPERATION_NOT_FOUND', 'No local MCP invocation for this request_id')
    request = json.loads((directory/'request.json').read_text())
    data = {'request_id': request_id, 'operation_id': identity, 'command': request['command'],
            'config': request['config'], 'log': str(directory/'stderr.log')}
    if (directory/'result.json').exists():
        result = json.loads((directory/'result.json').read_text())
        data.update(state='completed', result=result)
    else:
        # Match the receipt directory in the worker command, not just a possibly reused PID.
        pid = request.get('worker_pid')
        matched = False
        if pid:
            try:
                process = subprocess.run(['ps','-p',str(pid),'-o','args='], capture_output=True, text=True, timeout=5)
                matched = process.returncode == 0 and str(directory) in process.stdout and 'mcp_operations.py' in process.stdout
            except (OSError, subprocess.SubprocessError):
                pass
        data.update(state='pending' if matched else 'unknown', submitted_at=request['submitted_at'])
        if not matched:
            data['recovery'] = 'Local worker is not confirmed; inspect log and query remote job/client/round before resubmitting'
        # Completion can race with process exit.
        if (directory/'result.json').exists():
            data.update(state='completed',result=json.loads((directory/'result.json').read_text()))
            data.pop('recovery',None)
    return {'schema_version':1, 'ok':True, 'command':'mcp operation', 'data':data, 'error':None}


def submit(command, arguments, config, request_id):
    import fcntl
    os.umask(0o077)
    base = root()
    base.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(request_id.encode()).hexdigest()
    directory = base / identity
    config_path = Path(config) if config else None
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest() if config_path and config_path.is_file() else None
    expected = dict(command=command, arguments=arguments, config=config, config_sha256=config_hash, cwd=os.getcwd())
    with (base/'.lock').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        if directory.exists():
            request = json.loads((directory/'request.json').read_text())
            if request['input'] != expected:
                return error('REQUEST_CONFLICT', 'request_id is already bound to another command, configuration or arguments')
            return status(request_id)
        directory.mkdir(mode=0o700)
        write(directory/'request.json', dict(request_id=request_id, command=command, config=config,
              input=expected, submitted_at=time.time()))
        try:
            with (directory/'stderr.log').open('ab') as log:
                child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(directory)],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                _CHILDREN.append(child)
                request = json.loads((directory/'request.json').read_text())
                request['worker_pid'] = child.pid
                write(directory/'request.json', request)
        except OSError:
            write(directory/'result.json', error('OPERATION_LAUNCH_FAILED', 'Could not launch local CLI worker; inspect operation log'))
    return status(request_id)


def worker(directory):
    directory = Path(directory)
    request = json.loads((directory/'request.json').read_text())
    invocation = request['input']
    try:
        config = invocation['config']
        if config:
            path = Path(config)
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            if actual != invocation['config_sha256']:
                write(directory/'result.json', error('CONFIG_CHANGED', 'Configuration changed before CLI launch; no command executed'))
                return
        result = subprocess.run([sys.executable, '-m', 'afxx.cli', *invocation['arguments']],
                                cwd=invocation['cwd'], stdout=subprocess.PIPE, text=True)
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or type(payload.get('ok')) is not bool:
            raise ValueError('invalid CLI envelope')
        if result.returncode and payload['ok']:
            raise ValueError('CLI exit code conflicts with success response')
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        payload = error('CLI_PROTOCOL_ERROR', 'CLI response unavailable; inspect log and query remote state before retrying')
    write(directory/'result.json', payload)


if __name__ == '__main__':
    worker(sys.argv[1])
