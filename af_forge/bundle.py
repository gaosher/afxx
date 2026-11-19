#!/usr/bin/env python3
# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
"""Files required to run a workflow independently of the local repository."""

from pathlib import Path
import shutil

FILES = (
    "af_forge/cli.py",
    "af_forge/credentials.py",
    "af_forge/__init__.py",
    "af_forge/environment/__init__.py",
    "af_forge/afx/__init__.py",
    "af_forge/environment/python.py",
    "af_forge/environment/bootstrap.py",
    "af_forge/environment/cann.py",
    "af_forge/common.py",
    "af_forge/config.py",
    "af_forge/source.py",
    "af_forge/environment/runtime.py",
    "af_forge/execution.py",
    "af_forge/jobs.py",
    "af_forge/job_view.py",
    "af_forge/evidence_bundle.py",
    "af_forge/bundle.py",
    "af_forge/environment/consistency.py",
    "af_forge/afx/replay.py",
    "af_forge/afx/platform.py",
    "af_forge/afx/launcher.py",
    "af_forge/environment/package_sources.json",
    "af_forge/resources/config.example.yaml",
)


def copy_to(destination):
    destination = Path(destination)
    root = Path(__file__).resolve().parent.parent
    for name in FILES:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
