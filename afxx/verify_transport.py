"""Keep remote installation locks held while the local caller submits verify."""
import base64
import contextlib
import io
import json
from pathlib import Path
import selectors
import shlex
import subprocess
import zipfile

from afxx import common
from afxx.environment import runtime

# Only the lock/environment helpers travel to the remote host, never platform code.
MODULES = ('__init__.py', 'common.py', 'environment/__init__.py', 'environment/runtime.py')
BOOTSTRAP = '''import base64,contextlib,io,json,sys,tempfile,zipfile
payload=json.loads(sys.stdin.readline())
with tempfile.TemporaryDirectory(prefix='afxx-verify-probe-') as directory:
    zipfile.ZipFile(io.BytesIO(base64.b64decode(payload['code']))).extractall(directory)
    sys.path.insert(0,directory)
    from afxx import common
    from afxx.environment import runtime
    c=payload['config']
    try:
        with common.lock(runtime.venv_lock(c),shared=True), common.lock(runtime.registry(c).with_suffix('.lock'),shared=True):
            with contextlib.redirect_stdout(sys.stderr):
                output=common.capture([runtime.python(c),'-m','afx','status'],env=runtime.environment(c),timeout=60)
                response=json.loads(output)
                if response.get('ok') is False:
                    error=response.get('error') or {}
                    raise common.WorkflowError(error.get('code','PLATFORM_VERIFY_FAILED'),error.get('message','AFX status failed'))
                env=response.get('data',response)['environment']
                package=runtime.af_identity({'cann':env.get('cann_version'),'autofuse_binary':env.get('pyautofuse_sha256')})
            print(json.dumps({'data':package}),flush=True)
            # EOF on connection loss also releases both locks.
            sys.stdin.readline()
    except Exception as error:
        print(json.dumps({'error':{'code':getattr(error,'code','PLATFORM_VERIFY_FAILED'),'message':str(error)}}),flush=True)
'''


@contextlib.contextmanager
def installed_package(c):
    root = Path(__file__).resolve().parent
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in MODULES:
            archive.writestr('afxx/' + name, (root / name).read_bytes())
    argv = ['npussh', c['connection']['environment'],
            shlex.join([runtime.python(c), '-c', BOOTSTRAP])]
    process = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        process.stdin.write(json.dumps({'code': base64.b64encode(buffer.getvalue()).decode(),
                                       'config': c}) + '\n')
        process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(120):
                raise common.WorkflowError('REMOTE_UNAVAILABLE', 'verify package probe timed out; no verification submitted')
        response = json.loads(process.stdout.readline())
        if response.get('error'):
            error = response['error']
            raise common.WorkflowError(error['code'], error['message'])
        package = response['data']
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise common.WorkflowError('REMOTE_UNAVAILABLE', 'verify package probe unavailable; no verification submitted') from error
    else:
        yield package
    finally:
        if process is not None:
            # Closing stdin is the release signal, including when the POST fails.
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
