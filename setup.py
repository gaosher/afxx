"""Include repository-owned skills in the installed wheel."""
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        super().run()
        target = Path(self.build_lib) / "afxx" / "bundled_skills"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(Path(__file__).parent / "skills", target)


setup(cmdclass={"build_py": BuildPy})
