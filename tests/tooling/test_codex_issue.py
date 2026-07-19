"""Tests for scripts/codex-issue.

These tests create isolated environments with fake git and gh executables,
so they never access real GitHub or modify the real repository.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_executable(path: Path) -> None:
    st = path.stat()
    path.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_fake_bin(bin_dir: Path, name: str, content: str) -> Path:
    p = bin_dir / name
    p.write_text(content)
    _make_executable(p)
    return p


def _make_docs(project_dir: Path) -> None:
    docs_dir = project_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    dec_dir = docs_dir / "decisions"
    dec_dir.mkdir(parents=True, exist_ok=True)
    for fname in [
        "development-roadmap.md",
        "architecture.md",
        "data-model.md",
        "data-sources.md",
        "product-requirements.md",
    ]:
        (docs_dir / fname).write_text(f"(Placeholder: {fname})")
    (dec_dir / "ADR-001-test.md").write_text("# ADR-001")


def _run_script(
    script_path: Path,
    args: list[str],
    *,
    fake_bin: Path,
    project_dir: Path,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    if env_extra:
        env.update(env_extra)
    working_dir = str(cwd) if cwd is not None else str(project_dir)
    return subprocess.run(
        [str(script_path)] + args,
        capture_output=True,
        text=True,
        cwd=working_dir,
        env=env,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path) -> dict[str, Any]:
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    project_dir = tmp_path / "earnings-radar"
    project_dir.mkdir()
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "codex-issue"
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        capture_output=True,
        cwd=str(project_dir),
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:AlaxJiangX/earnings-radar.git"],
        capture_output=True,
        cwd=str(project_dir),
        check=True,
    )
    (project_dir / "AGENTS.md").write_text("# AGENTS.md\n\nRules here.\n")
    _make_docs(project_dir)
    return {"fake_bin": fake_bin, "project_dir": project_dir, "script": script_path}


# ---------------------------------------------------------------------------
# fake commands
# ---------------------------------------------------------------------------


def _make_gh(
    fake_bin: Path,
    *,
    state: str = "OPEN",
    title: str = "Test Issue Title",
    body: str = "Issue body text.",
    labels_str: str = "[]",
    url: str = "https://github.com/AlaxJiangX/earnings-radar/issues/42",
    stderr_text: str = "",
    fail_exit: int = 0,
) -> Path:
    gh_lines = [
        "#!/usr/bin/env python3",
        "import sys, json",
        "args = sys.argv[1:]",
        "if len(args) < 2 or args[0] != 'issue' or args[1] != 'view':",
        "    print('Unexpected gh args', file=sys.stderr); sys.exit(1)",
        "issue_num = args[2]",
        "json_field = None",
        "i = 3",
        "while i < len(args):",
        "    if args[i] == '--json': json_field = args[i+1]; i += 2",
        "    elif args[i] == '--jq': i += 2",
        "    elif args[i] == '--repo': i += 2",
        "    else: i += 1",
        "if issue_num == '99999':",
        "    print('issue not found', file=sys.stderr); sys.exit(1)",
        f"if {fail_exit} != 0:",
        f"    print({json.dumps(stderr_text)}, file=sys.stderr); sys.exit({fail_exit})",
    ]
    if stderr_text:
        gh_lines.append(f"print({json.dumps(stderr_text)}, file=sys.stderr)")
    gh_lines += [
        "fields = {",
        f"    'state': {json.dumps(state)},",
        f"    'title': {json.dumps(title)},",
        f"    'body': {json.dumps(body)},",
        f"    'url': {json.dumps(url)},",
        f"    'labels': {labels_str},",
        "}",
        "if json_field and json_field in fields:",
        "    v = fields[json_field]",
        "    if isinstance(v, str): print(v)",
        "    else: print(json.dumps(v))",
        "else: print(json.dumps(fields))",
    ]
    return _write_fake_bin(fake_bin, "gh", "\n".join(gh_lines) + "\n")


def _make_git(fake_bin: Path) -> Path:
    import shutil

    real_git = shutil.which("git") or "/usr/bin/git"
    return _write_fake_bin(fake_bin, "git", "#!/usr/bin/env bash\nexec " + real_git + ' "$@"\n')


# ---------------------------------------------------------------------------
# test case helper
# ---------------------------------------------------------------------------


class CodexIssueCase:
    def __init__(self, env: dict[str, Any]) -> None:
        self.env = env
        self.gh_state = "OPEN"
        self.gh_title = "Test Issue Title"
        self.gh_body = "Line 1 of body.\nLine 2 of body."
        self.gh_labels = "[]"
        self.gh_url = "https://github.com/AlaxJiangX/earnings-radar/issues/42"
        self.issue_num = "42"
        self.extra_args: list[str] = []
        self.subdir: Path | None = None
        self.gh_stderr = ""
        self.gh_fail_exit = 0

    def run(self) -> subprocess.CompletedProcess[str]:
        _make_git(self.env["fake_bin"])
        _make_gh(
            self.env["fake_bin"],
            state=self.gh_state,
            title=self.gh_title,
            body=self.gh_body,
            labels_str=self.gh_labels,
            url=self.gh_url,
            stderr_text=self.gh_stderr,
            fail_exit=self.gh_fail_exit,
        )
        cwd = self.env["project_dir"]
        if self.subdir:
            cwd = cwd / self.subdir
            cwd.mkdir(parents=True, exist_ok=True)
        return _run_script(
            self.env["script"],
            [self.issue_num] + self.extra_args,
            fake_bin=self.env["fake_bin"],
            project_dir=self.env["project_dir"],
            cwd=cwd,
        )


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestHelpAndUsage:
    def test_help_flag(self, isolated_env: dict[str, Any]) -> None:
        r = _run_script(
            isolated_env["script"],
            ["--help"],
            fake_bin=isolated_env["fake_bin"],
            project_dir=isolated_env["project_dir"],
        )
        assert r.returncode == 0
        assert "Usage" in r.stdout or "codex-issue" in r.stdout

    def test_help_short_flag(self, isolated_env: dict[str, Any]) -> None:
        r = _run_script(
            isolated_env["script"],
            ["-h"],
            fake_bin=isolated_env["fake_bin"],
            project_dir=isolated_env["project_dir"],
        )
        assert r.returncode == 0

    def test_no_args_shows_help(self, isolated_env: dict[str, Any]) -> None:
        r = _run_script(
            isolated_env["script"],
            [],
            fake_bin=isolated_env["fake_bin"],
            project_dir=isolated_env["project_dir"],
        )
        assert r.returncode == 0

    def test_non_numeric_issue(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.issue_num = "abc"
        r = c.run()
        assert r.returncode != 0
        assert "positive integer" in r.stderr.lower()

    def test_zero_issue(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.issue_num = "0"
        r = c.run()
        assert r.returncode != 0

    def test_negative_issue(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.issue_num = "-5"
        r = c.run()
        assert r.returncode != 0

    def test_extra_unknown_arg(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.extra_args.append("bogus")
        r = c.run()
        assert r.returncode != 0

    def test_unknown_flag(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.extra_args.append("--unknown")
        r = c.run()
        assert r.returncode != 0


class TestRepoValidation:
    def test_valid_repo_override(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.extra_args = ["--repo", "OtherOrg/other-repo"]
        r = c.run()
        assert r.returncode == 0
        assert "OtherOrg/other-repo" in r.stdout

    def test_repo_missing_slash(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.extra_args = ["--repo", "owner"]
        r = c.run()
        assert r.returncode != 0
        assert "Invalid" in r.stderr

    def test_repo_starts_with_slash(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.extra_args = ["--repo", "/owner/repo"]
        r = c.run()
        assert r.returncode != 0

    def test_repo_ends_with_slash(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.extra_args = ["--repo", "owner/"]
        r = c.run()
        assert r.returncode != 0

    def test_repo_extra_path(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.extra_args = ["--repo", "owner/repo/extra"]
        r = c.run()
        assert r.returncode != 0

    def test_repo_url_rejected(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.extra_args = ["--repo", "https://github.com/owner/repo"]
        r = c.run()
        assert r.returncode != 0

    def test_repo_with_space(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.extra_args = ["--repo", "owner repo/repo"]
        r = c.run()
        assert r.returncode != 0


class TestPreflightErrors:
    def test_not_in_git_repo(self, isolated_env: dict[str, Any], tmp_path: Path) -> None:
        non_repo = tmp_path / "not_a_repo"
        non_repo.mkdir()
        _make_git(isolated_env["fake_bin"])
        _make_gh(isolated_env["fake_bin"])
        r = _run_script(
            isolated_env["script"],
            ["42"],
            fake_bin=isolated_env["fake_bin"],
            project_dir=isolated_env["project_dir"],
            cwd=non_repo,
        )
        assert r.returncode != 0

    def test_no_gh_available(self, isolated_env: dict[str, Any]) -> None:
        _make_git(isolated_env["fake_bin"])
        r = _run_script(
            isolated_env["script"],
            ["42"],
            fake_bin=isolated_env["fake_bin"],
            project_dir=isolated_env["project_dir"],
        )
        assert r.returncode != 0
        assert "gh" in r.stderr.lower()


class TestIssueFetching:
    def test_open_issue_success(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_title = "Add widget"
        c.gh_body = "Implement GET /api/widget."
        r = c.run()
        assert r.returncode == 0
        assert "Add widget" in r.stdout
        assert "Implement GET /api/widget." in r.stdout
        assert "#42" in r.stdout
        assert "CODEX TASK BRIEF" in r.stdout

    def test_issue_body_empty(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_body = ""
        r = c.run()
        assert r.returncode == 0
        assert "has no body" in r.stdout

    def test_issue_url_in_output(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        r = c.run()
        assert r.returncode == 0
        assert "github.com/AlaxJiangX/earnings-radar/issues/42" in r.stdout

    def test_labels_in_output(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_labels = '["bug","enhancement"]'
        r = c.run()
        assert r.returncode == 0
        assert "bug" in r.stdout

    def test_labels_empty(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_labels = "[]"
        r = c.run()
        assert r.returncode == 0
        assert "(none)" in r.stdout

    def test_closed_issue_rejected(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_state = "CLOSED"
        r = c.run()
        assert r.returncode != 0
        assert "CLOSED" in r.stderr

    def test_gh_failure(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.issue_num = "99999"
        r = c.run()
        assert r.returncode != 0
        assert "gh failed" in r.stderr.lower()

    # -- _gh_json stderr separation tests --

    def test_gh_stderr_warning_not_in_title(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_title = "Clean Title"
        c.gh_stderr = "warning: deprecated API version"
        r = c.run()
        assert r.returncode == 0
        assert "Clean Title" in r.stdout
        assert "deprecated API version" not in r.stdout

    def test_gh_stderr_warning_not_in_body(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_body = "Body content."
        c.gh_stderr = "warning: something"
        r = c.run()
        assert r.returncode == 0
        assert "Body content." in r.stdout
        assert "warning: something" not in r.stdout

    def test_gh_fail_with_stderr_only(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_stderr = "fatal: network error"
        c.gh_fail_exit = 1
        r = c.run()
        assert r.returncode != 0
        assert "gh failed" in r.stderr.lower()
        assert "network error" in r.stderr

    def test_gh_fail_exit_code_3(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_stderr = "error"
        c.gh_fail_exit = 1
        r = c.run()
        assert r.returncode == 3

    def test_gh_fail_no_output_file(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        out = isolated_env["project_dir"] / "should_not_exist.txt"
        c.extra_args = ["--output", str(out)]
        c.gh_stderr = "error"
        c.gh_fail_exit = 1
        r = c.run()
        assert r.returncode != 0
        assert not out.exists()


class TestOutputFile:
    def test_output_writes_file(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        out = isolated_env["project_dir"] / "task_brief.txt"
        c.extra_args = ["--output", str(out)]
        r = c.run()
        assert r.returncode == 0
        assert out.exists()
        assert "CODEX TASK BRIEF" in out.read_text()

    def test_output_existing_file_refused(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        out = isolated_env["project_dir"] / "task_brief.txt"
        out.write_text("existing")
        c.extra_args = ["--output", str(out)]
        r = c.run()
        assert r.returncode != 0
        assert "already exists" in r.stderr.lower()

    def test_output_missing_dir(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        out = isolated_env["project_dir"] / "nonexistent" / "task_brief.txt"
        c.extra_args = ["--output", str(out)]
        r = c.run()
        assert r.returncode != 0

    def test_output_path_with_spaces(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        out = isolated_env["project_dir"] / "path with spaces" / "task brief.txt"
        out.parent.mkdir()
        c.extra_args = ["--output", str(out)]
        r = c.run()
        assert r.returncode == 0
        assert out.exists()

    def test_output_missing_path_arg(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.extra_args = ["--output"]
        r = c.run()
        assert r.returncode != 0

    def test_output_equals_syntax(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        out = isolated_env["project_dir"] / "brief2.txt"
        c.extra_args = [f"--output={out}"]
        r = c.run()
        assert r.returncode == 0
        assert out.exists()

    # -- no-clobber --

    def test_existing_dir_refused(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        out = isolated_env["project_dir"] / "adir"
        out.mkdir()
        c.extra_args = ["--output", str(out)]
        r = c.run()
        assert r.returncode != 0

    def test_existing_symlink_refused(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        real = isolated_env["project_dir"] / "real.txt"
        real.write_text("real")
        out = isolated_env["project_dir"] / "link.txt"
        out.symlink_to(real)
        c.extra_args = ["--output", str(out)]
        r = c.run()
        assert r.returncode != 0
        assert real.read_text() == "real"


class TestSubdirectoryInvocation:
    def test_from_subdirectory(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.subdir = Path("deeply/nested/dir")
        r = c.run()
        assert r.returncode == 0
        assert "CODEX TASK BRIEF" in r.stdout


class TestShellInjectionSafety:
    def test_body_with_dollar_signs(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_body = "Use $HOME and ${PATH} in config."
        r = c.run()
        assert r.returncode == 0
        assert "$HOME" in r.stdout
        assert "${PATH}" in r.stdout

    def test_body_with_backticks(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_body = "Run `ls -la` to check."
        r = c.run()
        assert r.returncode == 0
        assert "`ls -la`" in r.stdout

    def test_body_with_quotes(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_body = 'He said "hello" and she said goodbye.'
        r = c.run()
        assert r.returncode == 0
        assert "He said" in r.stdout

    def test_body_with_shell_keywords(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_body = "if [ -f /etc/passwd ]; then echo ok; fi"
        r = c.run()
        assert r.returncode == 0
        assert "if [ -f /etc/passwd ]" in r.stdout

    def test_body_with_multiline_and_tabs(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_body = "Line 1\n\nLine 3\twith tab\nLine 4"
        r = c.run()
        assert r.returncode == 0
        assert "Line 1" in r.stdout
        assert "with tab" in r.stdout


class TestExitCodes:
    def test_success_exit_0(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        r = c.run()
        assert r.returncode == 0

    def test_arg_error_exit_nonzero(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.issue_num = "-1"
        r = c.run()
        assert r.returncode != 0

    def test_closed_issue_exit_3(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.gh_state = "CLOSED"
        r = c.run()
        assert r.returncode == 3

    def test_file_exists_exit_4(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        out = isolated_env["project_dir"] / "exists.txt"
        out.write_text("old")
        c.extra_args = ["--output", str(out)]
        r = c.run()
        assert r.returncode == 4


class TestGeneratedContentStructure:
    def test_required_sections(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        r = c.run()
        assert r.returncode == 0
        for section in [
            "Task Source",
            "GitHub Issue Body",
            "Project Status Snapshot",
            "Required Reading",
            "Pre-Operation Git Verification",
            "Objective",
            "Allowed Scope",
            "Prohibited Actions",
            "Implementation Requirements",
            "Tests and Quality Checks",
            "Post-Completion Git Verification",
            "Final Report Format",
        ]:
            assert section in r.stdout, f"Missing section: {section}"

    def test_agents_md_mentioned(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        r = c.run()
        assert r.returncode == 0
        assert "AGENTS.md" in r.stdout


class TestRepoOverride:
    def test_repo_override(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.extra_args = ["--repo", "OtherOrg/other-repo"]
        r = c.run()
        assert r.returncode == 0
        assert "OtherOrg/other-repo" in r.stdout

    def test_repo_override_equals(self, isolated_env: dict[str, Any]) -> None:
        c = CodexIssueCase(isolated_env)
        c.extra_args = ["--repo=AnotherOrg/repo"]
        r = c.run()
        assert r.returncode == 0
        assert "AnotherOrg/repo" in r.stdout
