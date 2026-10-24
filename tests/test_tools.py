import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tools import (
    InvalidRepositoryUrlError,
    RepositoryTools,
    ToolError,
    clone_repository,
    normalize_github_url,
    remove_directory,
)

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


@pytest.mark.parametrize(
    ("input_url", "expected_url"),
    [
        ("https://github.com/psf/requests", "https://github.com/psf/requests"),
        ("https://github.com/psf/requests.git", "https://github.com/psf/requests"),
        ("  https://github.com/psf/requests/ ", "https://github.com/psf/requests"),
    ],
)
def test_normalize_github_url_accepts_valid_urls(input_url: str, expected_url: str) -> None:
    assert normalize_github_url(input_url) == expected_url


@pytest.mark.parametrize(
    "input_url",
    [
        "http://github.com/psf/requests",
        "https://gitlab.com/psf/requests",
        "https://github.com/psf",
        "https://github.com/psf/requests/tree/main",
        "file:///etc/passwd",
        "https://github.com/psf/..",
    ],
)
def test_normalize_github_url_rejects_invalid_urls(input_url: str) -> None:
    with pytest.raises(InvalidRepositoryUrlError):
        normalize_github_url(input_url)


def test_clone_repository_rejects_invalid_url_before_running_git(tmp_path: Path) -> None:
    with pytest.raises(InvalidRepositoryUrlError):
        clone_repository("not a url", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_list_files_shows_tracked_files_and_hides_git_directory(
    repository_tools: RepositoryTools,
) -> None:
    output = repository_tools.list_files()
    assert "README.md" in output
    assert ".github/workflows/ci.yml" in output
    assert ".git/" not in output


def test_list_files_filters_by_directory(repository_tools: RepositoryTools) -> None:
    output = repository_tools.list_files("src")
    assert "src/app.py" in output
    assert "README.md" not in output


def test_read_file_returns_numbered_lines(repository_tools: RepositoryTools) -> None:
    output = repository_tools.read_file("src/app.py")
    assert "lines 1-6 of 6" in output
    assert "    5 | def main():" in output


def test_read_file_redacts_secret_values(repository_tools: RepositoryTools) -> None:
    output = repository_tools.read_file("src/app.py")
    assert "hunter2-real-value" not in output
    assert "DATABASE_PASSWORD" in output
    assert "<REDACTED_VALUE" in output


def test_read_file_supports_line_window(repository_tools: RepositoryTools) -> None:
    output = repository_tools.read_file("src/app.py", start_line=5, max_lines=1)
    assert "lines 5-5 of 6" in output
    assert "start_line=6" in output


@pytest.mark.parametrize("unsafe_path", ["../outside.txt", "/etc/passwd", "src/../../outside"])
def test_read_file_rejects_paths_outside_repository(
    repository_tools: RepositoryTools, unsafe_path: str
) -> None:
    with pytest.raises(ToolError):
        repository_tools.read_file(unsafe_path)


def test_read_file_reports_missing_file(repository_tools: RepositoryTools) -> None:
    with pytest.raises(ToolError, match="File not found"):
        repository_tools.read_file("missing.py")


def test_read_file_does_not_print_binary_content(repository_tools: RepositoryTools) -> None:
    assert "binary file" in repository_tools.read_file("logo.bin")


def test_search_code_finds_matches_with_line_numbers(repository_tools: RepositoryTools) -> None:
    output = repository_tools.search_code(r"def main")
    assert "src/app.py:5:" in output


def test_search_code_respects_file_glob(repository_tools: RepositoryTools) -> None:
    output = repository_tools.search_code("ubuntu", file_glob="*.py")
    assert output.startswith("No matches")


def test_search_code_redacts_matched_secrets(repository_tools: RepositoryTools) -> None:
    output = repository_tools.search_code("password")
    assert "hunter2-real-value" not in output
    assert "src/app.py:3:" in output


def test_search_code_rejects_invalid_regex(repository_tools: RepositoryTools) -> None:
    with pytest.raises(ToolError, match="Invalid regular expression"):
        repository_tools.search_code("(unclosed")


def test_git_log_shows_commits_and_file_changes(repository_tools: RepositoryTools) -> None:
    output = repository_tools.git_log()
    assert "feat: initial commit" in output
    assert "src/app.py" in output


def test_git_log_filters_by_path(repository_tools: RepositoryTools) -> None:
    output = repository_tools.git_log(path="does-not-exist.txt")
    assert output.startswith("No commits found")


def test_run_ruff_reports_lint_issues(repository_tools: RepositoryTools) -> None:
    output = repository_tools.run_ruff()
    assert "F401" in output


def test_tools_do_not_modify_repository(
    repository_tools: RepositoryTools, sample_repository: Path
) -> None:
    repository_tools.list_files()
    repository_tools.read_file("README.md")
    repository_tools.search_code("Sample")
    repository_tools.git_log()
    repository_tools.run_ruff()
    status = subprocess.run(  # noqa: S603 - test helper with fixed arguments
        [GIT_EXECUTABLE, "-C", str(sample_repository), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""


def test_remove_directory_deletes_read_only_files(tmp_path: Path) -> None:
    target_directory = tmp_path / "to_delete"
    target_directory.mkdir()
    read_only_file = target_directory / "locked.pack"
    read_only_file.write_text("data", encoding="utf-8")
    os.chmod(read_only_file, stat.S_IREAD)
    remove_directory(target_directory)
    assert not target_directory.exists()


@pytest.mark.skipif(
    os.environ.get("RUN_NETWORK_TESTS") != "1", reason="set RUN_NETWORK_TESTS=1 to run"
)
def test_clone_repository_clones_public_repository(tmp_path: Path) -> None:
    clone_directory = clone_repository("https://github.com/octocat/Hello-World", tmp_path)
    try:
        assert (clone_directory / "README").is_file()
    finally:
        remove_directory(clone_directory)
