from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib

try:
    __version__ = version("verdantflare-app-google-flow")
except PackageNotFoundError:
    with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as source:
        __version__ = tomllib.load(source)["project"]["version"]
