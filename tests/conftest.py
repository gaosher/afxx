"""Register shared workflow fixtures for all test modules."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from test_afxx import cfg as cfg
from test_platform_adapter import candidate as candidate
from test_platform_adapter import platform_cfg as platform_cfg


def pytest_addoption(parser):
    parser.addoption(
        "--require-afx-contracts", action="store_true",
        help="Fail instead of skipping AFX contracts when AFX_SOURCE is missing (release validation).",
    )
