"""Read-only inspection tools that operate on a local clone of the target repository."""

import fnmatch
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

from redaction import redact_text

GITHUB_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9-]+)/(?P<repository>[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)
CLONE_DEPTH = 200
CLONE_TIMEOUT_SECONDS = 180
COMMAND_TIMEOUT_SECONDS = 60
MAX_OUTPUT_CHARACTERS = 12000
MAX_FILE_BYTES = 1_000_000
MAX_LINE_CHARACTERS = 400
DEFAULT_READ_LINES = 300
MAX_LISTED_FILES = 400
BINARY_SNIFF_BYTES = 8000


class ToolError(Exception):
    """Raised when a tool cannot complete; the message is passed back to the LLM."""


class InvalidRepositoryUrlError(ValueError):
    """Raised when the input is not a public GitHub repository URL."""


def normalize_github_url(repository_url: str) -> str:
    """Validate a GitHub URL and return it in the form https://github.com/owner/repository."""
    match = GITHUB_URL_PATTERN.match(repository_url.strip())
    if match is None:
        raise InvalidRepositoryUrlError(
            f"Not a GitHub repository URL: {repository_url!r}. "
            "Expected https://github.com/<owner>/<repository>"
        )
    owner = match.group("owner")
    repository = match.group("repository")
    if repository in (".", ".."):
        raise InvalidRepositoryUrlError(f"Invalid repository name in URL: {repository_url!r}")
    return f"https://github.com/{owner}/{repository}"


def truncate_output(text: str, limit: int = MAX_OUTPUT_CHARACTERS) -> str:
    """Cut long tool output so it fits in the LLM context, and say that it was cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[output truncated: showing {limit} of {len(text)} characters]"


def find_git_executable() -> str:
    git_path = shutil.which("git")
    if git_path is None:
        raise ToolError("git is not installed or not on PATH")
    return git_path


def build_git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_LFS_SKIP_SMUDGE"] = "1"
    return environment


def remove_directory(directory: Path) -> None:
    """Delete a directory tree, including read-only git object files on Windows."""
    if not directory.exists():
        return
    for current_root, directory_names, file_names in os.walk(directory):
        for name in directory_names + file_names:
            try:
                os.chmod(os.path.join(current_root, name), stat.S_IRWXU)
            except OSError:
                pass
    shutil.rmtree(directory, ignore_errors=True)


def clone_repository(repository_url: str, workspace_directory: Path) -> Path:
    """Shallow-clone a public GitHub repository into a new folder inside the workspace."""
    normalized_url = normalize_github_url(repository_url)
    workspace_directory.mkdir(parents=True, exist_ok=True)
    clone_directory = Path(tempfile.mkdtemp(prefix="target-", dir=workspace_directory))
    command = [
        find_git_executable(),
        "-c", "core.autocrlf=false",
        "-c", "core.symlinks=false",
        "-c", "core.longpaths=true",
        "-c", "core.fsmonitor=false",
        "clone",
        "--depth", str(CLONE_DEPTH),
        "--single-branch",
        "--no-tags",
        "--quiet",
        normalized_url,
        str(clone_directory),
    ]  # fmt: skip
    try:
        completed_process = subprocess.run(  # noqa: S603 - fixed git arguments, no shell
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLONE_TIMEOUT_SECONDS,
            env=build_git_environment(),
            check=False,
        )
    except subprocess.TimeoutExpired as timeout_error:
        remove_directory(clone_directory)
        raise ToolError(
            f"Cloning {normalized_url} timed out after {CLONE_TIMEOUT_SECONDS} seconds"
        ) from timeout_error
    if completed_process.returncode != 0:
        remove_directory(clone_directory)
        error_message = completed_process.stderr.strip() or "unknown git error"
        if "could not read Username" in error_message or "not found" in error_message.lower():
            error_message = "repository not found or not public"
        raise ToolError(f"Could not clone {normalized_url}: {error_message}")
    return clone_directory


class RepositoryTools:
    """Inspection tools scoped to one cloned repository. No tool writes to the repository."""

    def __init__(self, repository_root: Path) -> None:
        self.repository_root = repository_root.resolve()
        self._tracked_files_cache: list[str] | None = None

    def _run_git(self, arguments: list[str]) -> str:
        command = [find_git_executable(), "-C", str(self.repository_root), *arguments]
        try:
            completed_process = subprocess.run(  # noqa: S603 - fixed git arguments, no shell
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=COMMAND_TIMEOUT_SECONDS,
                env=build_git_environment(),
                check=False,
            )
        except subprocess.TimeoutExpired as timeout_error:
            raise ToolError(f"git {arguments[0]} timed out") from timeout_error
        if completed_process.returncode != 0:
            raise ToolError(f"git {arguments[0]} failed: {completed_process.stderr.strip()}")
        return completed_process.stdout

    def _clean_relative_path(self, relative_path: str) -> str:
        cleaned_path = relative_path.strip().replace("\\", "/")
        while cleaned_path.startswith("./"):
            cleaned_path = cleaned_path[2:]
        cleaned_path = cleaned_path.strip("/")
        if cleaned_path in ("", "."):
            return ""
        pure_path = PurePosixPath(cleaned_path)
        if pure_path.is_absolute() or ".." in pure_path.parts or ":" in pure_path.parts[0]:
            raise ToolError(f"Path must be relative to the repository root: {relative_path!r}")
        return cleaned_path

    def _resolve_inside_repository(self, relative_path: str) -> Path:
        cleaned_path = self._clean_relative_path(relative_path)
        resolved_path = (self.repository_root / cleaned_path).resolve()
        if not resolved_path.is_relative_to(self.repository_root):
            raise ToolError(f"Path escapes the repository: {relative_path!r}")
        return resolved_path

    def _tracked_files(self) -> list[str]:
        if self._tracked_files_cache is None:
            output = self._run_git(["ls-files", "-z"])
            tracked_files = []
            for file_path in output.split("\0"):
                if file_path != "":
                    tracked_files.append(file_path)
            self._tracked_files_cache = sorted(tracked_files)
        return self._tracked_files_cache

    @staticmethod
    def _is_binary(file_path: Path) -> bool:
        with open(file_path, "rb") as file_handle:
            return b"\0" in file_handle.read(BINARY_SNIFF_BYTES)

    def list_files(self, directory: str = "") -> str:
        """List files tracked by git under a directory, with their sizes in bytes."""
        directory_prefix = self._clean_relative_path(directory)
        matching_files = []
        for file_path in self._tracked_files():
            if directory_prefix == "" or file_path.startswith(directory_prefix + "/"):
                matching_files.append(file_path)
        if not matching_files:
            return f"No tracked files under {directory_prefix or 'the repository root'}"
        output_lines = [f"{len(matching_files)} tracked files under /{directory_prefix}:"]
        for file_path in matching_files[:MAX_LISTED_FILES]:
            absolute_path = self.repository_root / file_path
            if absolute_path.is_file():
                output_lines.append(f"{file_path} ({absolute_path.stat().st_size} bytes)")
            else:
                output_lines.append(f"{file_path} (not present in working tree)")
        if len(matching_files) > MAX_LISTED_FILES:
            hidden_count = len(matching_files) - MAX_LISTED_FILES
            output_lines.append(f"[{hidden_count} more files not shown; list a subdirectory]")
        return truncate_output("\n".join(output_lines))

    def read_file(self, path: str, start_line: int = 1, max_lines: int = DEFAULT_READ_LINES) -> str:
        """Return numbered lines of a text file. Secret values are redacted."""
        file_path = self._resolve_inside_repository(path)
        if not file_path.is_file():
            raise ToolError(f"File not found: {path!r}")
        file_size = file_path.stat().st_size
        if file_size > MAX_FILE_BYTES:
            raise ToolError(f"File too large to read ({file_size} bytes): {path!r}")
        if self._is_binary(file_path):
            return f"{path} is a binary file ({file_size} bytes); contents not shown"
        file_text = file_path.read_text(encoding="utf-8", errors="replace")
        all_lines = redact_text(file_text).split("\n")
        if all_lines and all_lines[-1] == "":
            all_lines.pop()
        total_lines = len(all_lines)
        first_line = max(1, start_line)
        last_line = min(total_lines, first_line + max(1, max_lines) - 1)
        relative_name = file_path.relative_to(self.repository_root).as_posix()
        output_lines = [f"File: {relative_name} (lines {first_line}-{last_line} of {total_lines})"]
        for line_number in range(first_line, last_line + 1):
            line_text = all_lines[line_number - 1][:MAX_LINE_CHARACTERS]
            output_lines.append(f"{line_number:>5} | {line_text}")
        if last_line < total_lines:
            output_lines.append(f"[more lines remain; call again with start_line={last_line + 1}]")
        return truncate_output("\n".join(output_lines))

    def search_code(
        self,
        pattern: str,
        file_glob: str | None = None,
        case_sensitive: bool = False,
        max_results: int = 80,
    ) -> str:
        """Search tracked text files for a regular expression. Matching lines are redacted."""
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled_pattern = re.compile(pattern, flags)
        except re.error as regex_error:
            raise ToolError(
                f"Invalid regular expression {pattern!r}: {regex_error}"
            ) from regex_error
        results = []
        for file_path in self._tracked_files():
            if file_glob and not (
                fnmatch.fnmatch(file_path, file_glob)
                or fnmatch.fnmatch(PurePosixPath(file_path).name, file_glob)
            ):
                continue
            absolute_path = self.repository_root / file_path
            if not absolute_path.is_file() or absolute_path.stat().st_size > MAX_FILE_BYTES:
                continue
            if self._is_binary(absolute_path):
                continue
            file_text = absolute_path.read_text(encoding="utf-8", errors="replace")
            for line_number, line_text in enumerate(file_text.split("\n"), start=1):
                if compiled_pattern.search(line_text[:2000]):
                    snippet = redact_text(line_text.strip())[:MAX_LINE_CHARACTERS]
                    results.append(f"{file_path}:{line_number}: {snippet}")
                    if len(results) >= max_results:
                        results.append(f"[stopped after {max_results} matches]")
                        return truncate_output("\n".join(results))
        if not results:
            return f"No matches for {pattern!r}"
        return truncate_output("\n".join(results))

    def git_log(self, max_commits: int = 30, path: str | None = None) -> str:
        """Show recent commits with the files each one added, modified, or deleted."""
        commit_limit = min(max(1, max_commits), 100)
        arguments = [
            "log",
            f"-n{commit_limit}",
            "--date=short",
            "--format=commit %h | %ad | %an | %s",
            "--name-status",
        ]
        if path:
            arguments.extend(["--", self._clean_relative_path(path)])
        log_output = self._run_git(arguments).strip()
        if log_output == "":
            return "No commits found" + (f" touching {path!r}" if path else "")
        is_shallow = self._run_git(["rev-parse", "--is-shallow-repository"]).strip() == "true"
        header = "Status letters: A=added, M=modified, D=deleted, R=renamed."
        if is_shallow:
            header += f" History is shallow (only the latest {CLONE_DEPTH} commits were cloned)."
        return truncate_output(header + "\n" + redact_text(log_output))

    def run_ruff(self, path: str = "") -> str:
        """Run the Ruff linter (static, never executes repository code) using the repo's config."""
        target_path = self._clean_relative_path(path) or "."
        self._resolve_inside_repository(target_path)
        command = [
            sys.executable, "-m", "ruff", "check",
            "--no-cache", "--exit-zero", "--output-format", "concise",
            target_path,
        ]  # fmt: skip
        try:
            completed_process = subprocess.run(  # noqa: S603 - fixed arguments, no shell
                command,
                cwd=self.repository_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as timeout_error:
            raise ToolError("ruff timed out") from timeout_error
        combined_output = (completed_process.stdout + completed_process.stderr).strip()
        if "No module named ruff" in combined_output:
            raise ToolError("ruff is not installed in the agent environment")
        if combined_output == "":
            combined_output = "Ruff reported no issues"
        return truncate_output(redact_text(combined_output))
