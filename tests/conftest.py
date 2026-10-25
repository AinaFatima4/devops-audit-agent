"""Shared pytest fixtures: a small git repository built fresh for each test."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tools import RepositoryTools

GIT_EXECUTABLE = shutil.which("git")


def run_git(repository_root: Path, *arguments: str) -> None:
    subprocess.run(  # noqa: S603 - test helper with fixed arguments
        [GIT_EXECUTABLE, "-C", str(repository_root), *arguments],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def sample_repository(tmp_path: Path) -> Path:
    repository_root = tmp_path / "sample"
    (repository_root / "src").mkdir(parents=True)
    (repository_root / ".github" / "workflows").mkdir(parents=True)
    (repository_root / "README.md").write_text("# Sample\n", encoding="utf-8")
    (repository_root / "src" / "app.py").write_text(
        'import os\n\nDATABASE_PASSWORD = "hunter2-real-value"\n\ndef main():\n    return 1\n',
        encoding="utf-8",
    )
    (repository_root / ".github" / "workflows" / "ci.yml").write_text(
        "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n", encoding="utf-8"
    )
    (repository_root / "logo.bin").write_bytes(b"\x89PNG\x00\x00binary")
    run_git(repository_root, "init", "--quiet")
    run_git(repository_root, "config", "user.email", "tester@example.com")
    run_git(repository_root, "config", "user.name", "Tester")
    run_git(repository_root, "config", "commit.gpgsign", "false")
    run_git(repository_root, "add", ".")
    run_git(repository_root, "commit", "--quiet", "-m", "feat: initial commit")
    return repository_root


@pytest.fixture
def repository_tools(sample_repository: Path) -> RepositoryTools:
    return RepositoryTools(sample_repository)
