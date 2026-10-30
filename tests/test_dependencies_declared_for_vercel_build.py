"""Regression: Vercel's Python build installs deps from pyproject.toml only.

Production evidence (build log, deployment dpl_GshVuB6W6mfACs87WQyAq7zLPtSD):
"Installing required dependencies from pyproject.toml..." followed by a
2s build with no package installation. requirements.txt was never read, so
curl_cffi was absent at runtime, VoxHttpClient raised ImportError, and the
handler returned {"error": "internal error"}.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_vercel_build_reads_curl_cffi_dependency():
    project = _pyproject()["project"]
    deps = project["dependencies"]
    assert any(
        dep.startswith("curl_cffi") for dep in deps
    ), "curl_cffi must be declared in [project].dependencies — Vercel installs only from pyproject.toml"


def test_vercel_python_version_supported():
    assert _pyproject()["project"]["requires-python"] == ">=3.12"


def test_requirements_txt_kept_in_sync():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "curl_cffi" in req
    pyproject_deps = _pyproject()["project"]["dependencies"]
    for dep in pyproject_deps:
        name = dep.split(">")[0].split("=")[0].split("<")[0].strip()
        assert name in req, f"{name} missing from requirements.txt"


def test_vercel_entrypoint_preserved():
    assert _pyproject()["tool"]["vercel"]["entrypoint"] == "api.check:handler"
