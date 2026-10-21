"""Copy canonical root-level browser assets into built Python packages only.

The source checkout has no second editable copy under src. Setuptools invokes
this command for both a wheel built directly and a wheel built from the sdist.
"""
from pathlib import Path

from setuptools.command.build_py import build_py


ROOT = Path(__file__).resolve().parents[1]
ASSET_NAMES = ("graphmind.css", "graphmind.js")


class BuildPy(build_py):
    def run(self):
        super().run()
        destination = Path(self.build_lib) / "graphmind/web/static"
        self.mkpath(str(destination))
        for name in ASSET_NAMES:
            source = ROOT / "assets/web" / name
            if not source.is_file():
                raise FileNotFoundError(f"Required browser asset is missing: {name}")
            self.copy_file(str(source), str(destination / name))

    def get_outputs(self, include_bytecode=1):
        outputs = super().get_outputs(include_bytecode)
        return outputs + [str(Path(self.build_lib) / "graphmind/web/static" / name)
                          for name in ASSET_NAMES]
