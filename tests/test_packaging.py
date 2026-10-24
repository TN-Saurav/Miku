import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_requirements_mirror_pyproject_runtime_dependencies():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"(?ms)^dependencies\s*=\s*\[\s*(.*?)^\]", pyproject)
    assert match is not None
    project_dependencies = re.findall(r'"([^"\n]+)"', match.group(1))
    requirements = [
        line.strip()
        for line in (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert requirements == project_dependencies
    assert "." not in requirements
    assert not any("pytest" in item.casefold() for item in requirements)


def test_aclclouds_entrypoint_runs_from_parent_directory_without_credentials(tmp_path):
    project = tmp_path / "Miku-main"
    project.mkdir()
    shutil.copy2(PROJECT_ROOT / "aclclouds.py", project / "aclclouds.py")
    shutil.copytree(PROJECT_ROOT / "miku", project / "miku")
    env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if key in os.environ
    }

    result = subprocess.run(
        [sys.executable, str(project / "aclclouds.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 2
    assert "DISCORD_BOT_TOKEN is required" in result.stderr
    assert "Connecting to Discord" not in result.stderr
