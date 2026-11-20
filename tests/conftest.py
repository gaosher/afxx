"""Register shared workflow fixtures for all test modules."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from test_af_forge import cfg as cfg
from test_platform_adapter import candidate as candidate
from test_platform_adapter import platform_cfg as platform_cfg
