"""Opt-in deploy lifecycle coordination; AFX owns execution and process shutdown."""
from contextlib import contextmanager
import os
from pathlib import Path

from afxx import common
from afxx.afx import launcher
from afxx.environment import runtime


class Coordination:
    def __init__(self, c, job, before):
        self.c, self.job, self.before = c, job, before
        self.home = before.get('home')
        self.install_started = False
        self.installed = False
        self.record = {'state': 'checking', 'before': before,
                       'restore_mode': 'new_session' if before['state'] == 'running' else before['state']}

    def save(self, state, **fields):
        self.record.update(state=state, **fields)
        common.update_record(self.job/'result.json', client_coordination=self.record)

    def control(self, action, **kwargs):
        return launcher.control(self.c, action, home=self.home, lock_held=True, **kwargs)

    def validate(self, before=None):
        before = self.before if before is None else before
        common.require_code(before['state'] in ('running', 'idle', 'stopped', 'uninitialized')
            and not before.get('active_operation'), 'DEPLOY_CLIENT_BUSY',
            'client is paused, transitioning, or has an active operation; no installation performed')
        if not before.get('daemon_running'):
            return
        common.require_code(all(r['state'] == 'running' and
            r.get('spec', {}).get('environment', {}).get('id') == 'local'
            for r in before.get('active_runs', [])), 'DEPLOY_CLIENT_UNSUPPORTED',
            'only stable local execution sessions can be coordinated')
        manifest = before['execution_environment']
        root = manifest.get('env', {}).get('ASCEND_HOME_PATH')
        common.require_code(root and Path(root).resolve() ==
            (Path(self.c['runtime']['install_root'])/'cann').resolve(),
            'DEPLOY_CLIENT_ENVIRONMENT_MISMATCH', 'registered client CANN differs from deploy target')

    def restore(self):
        self.save('restoring')
        common.phase(self.job, 'restore_client')
        result = self.control('start', daemon_only=self.before['state'] == 'idle')
        common.require_code(result['state'] == self.before['state'], 'DEPLOY_CLIENT_RESTORE_FAILED',
                            'client did not return to its original execution state')
        observed = self.control('status', inspect=True)
        common.require_code(observed['state'] == self.before['state'], 'DEPLOY_CLIENT_RESTORE_FAILED',
                            'client state changed during restoration')
        self.validate(observed)
        manifest = observed['execution_environment']
        if self.installed:
            # Probe the registered execution interpreter and env, not the submitting CLI.
            env = {k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV')}
            env.update(manifest['env'])
            evidence = runtime.probe(self.c, env, self.job, interpreter=manifest['python'])
            deployed = common.read(self.job/'result.json')['runtime']
            common.require_code(all(evidence[k] == deployed[k] for k in ('pyautofuse','adapter','codegen')),
                                'DEPLOY_CLIENT_ENVIRONMENT_MISMATCH', 'restored execution AF differs from deployment')
        self.save('restored', after=observed)


@contextmanager
def coordinate(c, job):
    request = common.read(job/'request.json') if (job/'request.json').exists() else {}
    if not request.get('coordinate_client'):
        yield None
        return
    before = launcher.control(c, 'status')
    if not before.get('daemon_running'):
        controller = Coordination(c, job, before)
        controller.validate()
        controller.save('unchanged')
        yield controller
        return
    home = before['home']
    # Serialize AFXX lifecycle calls across stop/install/restore. External AFX callers
    # must also refrain from starting sessions; this is not an AFX admission fence.
    with common.lock(Path(home)/'launch.lock'):
        before = launcher.control(c, 'status', home=home, lock_held=True, inspect=True)
        controller = Coordination(c, job, before)
        controller.save('checking')
        try:
            controller.validate()
        except BaseException as error:
            controller.save('blocked', error=str(error))
            raise
        if not before.get('daemon_running'):
            controller.save('unchanged')
            yield controller
            return
        controller.save('stopping')
        common.phase(job, 'stop_client')
        try:
            stopped = controller.control('stop')
            common.require_code(stopped['state'] == 'stopped' and not stopped['daemon_running'],
                                'DEPLOY_CLIENT_STOP_FAILED', 'client has not exited; installation aborted')
        except BaseException as error:
            controller.save('stop_failed', error=str(error))
            raise
        controller.save('stopped')
        try:
            yield controller
        except BaseException as error:
            if controller.install_started:
                controller.save('held_stopped', error=str(error), reason='installation started but was not verified')
            else:
                try:
                    controller.restore()
                except BaseException as restore_error:
                    controller.save('restore_failed', error=str(restore_error), deploy_error=str(error))
            raise
        else:
            try:
                controller.restore()
            except BaseException as error:
                cleanup = None
                try:
                    cleanup = controller.control('stop')
                except Exception as stop_error:
                    cleanup = {'error': str(stop_error)}
                controller.save('restore_failed', error=str(error), cleanup=cleanup)
                raise common.WorkflowError('DEPLOY_CLIENT_RESTORE_FAILED',
                    'AF installation verified, but client restoration failed; inspect result.json before retrying') from error
