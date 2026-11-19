# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
"""Local credential preparation and post-init delivery over SSH stdin."""

import getpass
import inspect
import json
from pathlib import Path
import shlex
import subprocess
import sys
import warnings

from af_forge import common, config
from af_forge.afx import platform


def install_files(paths, token):
    """Shared local/remote writer: never replace a different credential."""
    import os
    from pathlib import Path
    import tempfile
    paths = list(dict.fromkeys(Path(p) for p in paths))
    for path in paths:
        if not path.is_absolute() or path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise ValueError("unsafe credential path")
        if path.exists() and (not path.is_file() or path.read_text().strip() != token):
            raise ValueError("credential conflict")
    for path in paths:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.exists():
            path.chmod(0o600)
            continue
        fd, name = tempfile.mkstemp(prefix=".token-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(token + "\n")
            try:
                os.link(name, path)
            except FileExistsError:
                if path.is_symlink() or path.read_text().strip() != token:
                    raise ValueError("credential conflict")
                path.chmod(0o600)
        finally:
            os.unlink(name)


def read_token(c, *, prompt=False):
    p = config.platform_settings(c)
    path = Path(p["token_file"])
    common.require_code(not common.inside(path.resolve(), Path(c["local"]["source_dir"]).resolve()),
                        "PLATFORM_CREDENTIAL_INVALID", "platform.token_file 必须位于同步源码目录之外")
    try:
        if path.exists():
            token = path.read_text().strip()
        else:
            common.require_code(prompt and sys.stdin.isatty(), "PLATFORM_CREDENTIAL_MISSING",
                                "缺少 platform.token_file；请在交互终端运行 env init 隐藏输入，或预先创建该文件（0600）。")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass("AFX 平台 token（输入隐藏）: ").strip()
        common.require_code(bool(token) and not any(ch.isspace() for ch in token),
                            "PLATFORM_CREDENTIAL_INVALID", "平台 token 为空或包含空白字符")
        install_files([str(path)], token)
        return token
    except common.WorkflowError:
        raise
    except (OSError, ValueError, EOFError, getpass.GetPassWarning):
        raise common.WorkflowError("PLATFORM_CREDENTIAL_INVALID", "无法安全读取或保存 platform.token_file；请检查路径和权限") from None


def prepare(c):
    if "platform" not in c:
        return
    if not c["platform"].get("client_token_file"):
        c["platform"]["client_token_file"] = c["platform"].get("remote_token_file")
    read_token(c, prompt=True)
    platform.call(c, "GET", "/api/me")


def sync(c):
    token = read_token(c)
    p = config.platform_settings(c)
    # The payload is stdin only: never argv, a job artifact, or captured diagnostics.
    code = inspect.getsource(install_files) + "\n" + """
import json,sys
try:
    payload=json.load(sys.stdin)
    install_files(payload['paths'],payload['token'])
    print(json.dumps({'ok':True}))
except ValueError:
    print(json.dumps({'ok':False,'code':'PLATFORM_CREDENTIAL_CONFLICT'}))
except OSError:
    print(json.dumps({'ok':False,'code':'PLATFORM_CREDENTIAL_SYNC_FAILED'}))
"""
    paths = list(dict.fromkeys([p["remote_token_file"], p.get("client_token_file") or p["remote_token_file"]]))
    try:
        result = subprocess.run(
            ["npussh", c["connection"]["environment"], shlex.join(["python3", "-c", code])],
            input=json.dumps({"paths": paths, "token": token}), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60, check=False,
        )
        value = json.loads(result.stdout) if result.returncode == 0 else {}
        if not value.get("ok"):
            code = "PLATFORM_CREDENTIAL_CONFLICT" if value.get("code") == "PLATFORM_CREDENTIAL_CONFLICT" else "PLATFORM_CREDENTIAL_SYNC_FAILED"
            raise common.WorkflowError(code, "远端 token 同步失败；检查连接、目录权限或已有不同凭据，不会覆盖不同 token。")
    except common.WorkflowError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise common.WorkflowError("PLATFORM_CREDENTIAL_SYNC_FAILED", "远端 token 同步失败；请查询同一 init job 重试。") from None
    return {"state": "ready", "local_file": p["token_file"], "remote_files": paths}


def complete_init(c, job_id, data):
    if data.get("state") != "success" or data.get("task_type") != "init":
        return data
    # Resume with the submission's frozen paths, not a subsequently edited config.
    frozen = Path(c["local"]["state_dir"]) / job_id / "config.yaml"
    if not frozen.is_file():
        if "platform" in c:
            raise common.WorkflowError("PLATFORM_CREDENTIAL_SYNC_FAILED", "缺少本地 init 冻结配置，无法确认凭据同步目标", {"job_id": job_id, "environment_state": "success"})
        return data
    submitted = config.config(frozen)
    if "platform" not in submitted:
        return data
    common.require_code(submitted["connection"] == c["connection"] and submitted["remote"] == c["remote"],
                        "PLATFORM_CREDENTIAL_SYNC_FAILED", "查询配置与 init 目标环境不一致")
    try:
        auth = sync(submitted)
    except common.WorkflowError as error:
        raise common.WorkflowError(error.code, str(error), {"job_id": job_id, "environment_state": "success"}) from None
    return dict(data, auth=auth)
