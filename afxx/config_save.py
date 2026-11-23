"""Opt-in local config writeback after successful remote environment updates."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from afxx import common, config


def _tree(text):
    common.require_code(not any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)),
                        'CONFIG_SAVE_UNSUPPORTED', '--save-config does not support YAML aliases')
    root = yaml.compose(text)
    def check(node):
        if isinstance(node, MappingNode):
            keys = [key.value for key, _ in node.value]
            common.require_code(len(keys) == len(set(keys)) and '<<' not in keys,
                                'CONFIG_SAVE_UNSUPPORTED', 'duplicate or merged YAML keys cannot be saved')
            for _, value in node.value:
                check(value)
        elif isinstance(node, SequenceNode):
            for value in node.value:
                check(value)
    common.require_code(isinstance(root, MappingNode), 'CONFIG_SAVE_UNSUPPORTED', 'config must be a YAML mapping')
    check(root)
    return root


def _replace(text, path, value):
    node = _tree(text)
    for index, key in enumerate(path):
        if isinstance(node, SequenceNode) and isinstance(key, int):
            node = node.value[key]
            continue
        common.require_code(isinstance(node, MappingNode), 'CONFIG_SAVE_UNSUPPORTED', 'expected mapping at ' + str(key))
        values = {k.value: v for k, v in node.value}
        if key in values:
            node = values[key]
            continue
        nested = value
        for part in reversed(path[index + 1:]):
            nested = {part: nested}
        entry = json.dumps(key) + ': ' + json.dumps(nested, ensure_ascii=False)
        if node.flow_style:
            pos = node.end_mark.index - 1
            return text[:pos] + (', ' if node.value else '') + entry + text[pos:]
        last = node.value[-1][1]
        pos = last.end_mark.index
        if last.end_mark.column:
            newline = text.find('\n', pos)
            pos = len(text) if newline < 0 else newline + 1
        prefix = '' if pos == 0 or text[pos-1] == '\n' else '\n'
        return text[:pos] + prefix + ' ' * node.value[0][0].start_mark.column + entry + '\n' + text[pos:]
    common.require_code(isinstance(node, ScalarNode), 'CONFIG_SAVE_UNSUPPORTED', 'version field must be scalar')
    return text[:node.start_mark.index] + json.dumps(value, ensure_ascii=False) + text[node.end_mark.index:]


def render(text, updates):
    raw = yaml.safe_load(text)
    for section, values in updates.items():
        for name, value in values.items():
            if section == 'python_env' and name == 'torch_version' and raw.get(section, {}).get('stable_requirements') is not None:
                requirements = raw[section]['stable_requirements']
                indices = [i for i, v in enumerate(requirements) if v.startswith('torch==')]
                common.require_code(len(indices) == 1, 'CONFIG_SAVE_UNSUPPORTED', 'expected one torch requirement')
                text = _replace(text, [section, 'stable_requirements', indices[0]], 'torch==' + value)
            elif isinstance(value, dict):
                for field, item in value.items():
                    # Optional advanced pins must not be left stale, but need not be introduced.
                    if field in ('version', 'sha256') and name == 'torch_npu' and field not in raw.get(section, {}).get(name, {}):
                        continue
                    text = _replace(text, [section, name, field], item)
            else:
                text = _replace(text, [section, name], value)
    config.validate_config(config.normalize_config(yaml.safe_load(text)))
    return text


def prepare(path, selection):
    common.require_code(bool(selection), 'INVALID_ARGUMENT', '--save-config requires an update component')
    path = Path(path).expanduser().resolve()
    text = path.read_text()
    _tree(text)
    return {'path': str(path), 'sha256': hashlib.sha256(text.encode()).hexdigest()}


def _complete(c, job_id, data, report):
    if data.get('state') != 'success':
        return data
    request_path = Path(c['local']['state_dir']) / job_id / 'request.json'
    if not request_path.is_file():
        return data
    request = common.read(request_path)
    intent = request.get('save_config')
    if request.get('command') != 'update' or not intent:
        return data
    with common.lock(request_path.with_suffix('.config-save.lock')):
        request = common.read(request_path)
        if request.get('config_save_result'):
            return dict(data, config_save=request['config_save_result'])
        frozen = config.config(request_path.with_name('config.yaml'))
        common.require_code(frozen['connection'] == c['connection'] and frozen['remote'] == c['remote'],
                            'CONFIG_SAVE_CONFLICT', 'query environment differs from update target')
        updates = report.get('config_updates')
        common.require_code(bool(updates), 'CONFIG_SAVE_FAILED', 'successful update did not return resolved versions')
        path = Path(intent['path'])
        with common.lock(Path(c['local']['state_dir']) / ('config-' + common.digest(str(path)) + '.lock')):
            text = path.read_text()
            new = render(text, updates)
            digest = hashlib.sha256(text.encode()).hexdigest()
            if digest != intent['sha256']:
                # A retry after replacement but before receipt persistence is harmless.
                common.require_code(yaml.safe_load(text) == yaml.safe_load(new), 'CONFIG_SAVE_CONFLICT',
                                    'config changed since submission; environment updated, local config not overwritten')
            elif new != text:
                fd, temp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
                try:
                    with os.fdopen(fd, 'w') as stream:
                        stream.write(new)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temp, path.stat().st_mode & 0o777)
                    common.require_code(hashlib.sha256(path.read_bytes()).hexdigest() == digest, 'CONFIG_SAVE_CONFLICT',
                                        'config changed while saving; local edits preserved')
                    os.replace(temp, path)
                finally:
                    Path(temp).unlink(missing_ok=True)
            receipt = {'state': 'saved', 'path': str(path), 'fields': updates}
            common.update_record(request_path, config_save_result=receipt)
        return dict(data, config_save=receipt)


def complete(c, job_id, data, report):
    try:
        return _complete(c, job_id, data, report)
    except common.WorkflowError as error:
        error.data = {"job_id": job_id, "environment_state": "success", "config_save": "failed"}
        raise
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise common.WorkflowError("CONFIG_SAVE_FAILED", "环境已更新，本地配置回写失败：" + type(error).__name__,
                                   {"job_id": job_id, "environment_state": "success"}) from None
