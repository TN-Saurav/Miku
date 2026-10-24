import re
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


def test_python_module_and_console_script_use_shared_startup_function():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    module_entrypoint = (PROJECT_ROOT / "miku" / "__main__.py").read_text(encoding="utf-8")

    assert '[project.scripts]\nmiku = "miku.bot:main"' in pyproject
    assert "from .bot import main" in module_entrypoint
    assert "raise SystemExit(main())" in module_entrypoint
