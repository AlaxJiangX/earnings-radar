# Codex Development Workflow — Earnings Radar

This document describes how to start a safe, structured development task from a
GitHub Issue using Codex and the `codex-issue` tool.

## Permanent Rules (AGENTS.md)

All permanent development rules live in [AGENTS.md](/AGENTS.md). Every task
must start by reading AGENTS.md and the required project documents. The
`codex-issue` script enforces this in generated briefs.

## Prerequisites

- Git installed and configured
- [GitHub CLI](https://cli.github.com/) (`gh`) installed and authenticated
  ```bash
  gh auth status
  ```
- [Codex CLI](https://github.com/openai/codex) installed
- Docker and Docker Compose (for running quality checks)

## Generating a Task Brief

From the repository root:

```bash
./scripts/codex-issue <issue-number>
```

This reads the GitHub Issue and prints a structured task brief to stdout.

### Examples

```bash
# Print brief to terminal
./scripts/codex-issue 12

# Write brief to a file
./scripts/codex-issue 12 --output task-brief.txt

# Override the repository (if remote isn't the GitHub repo)
./scripts/codex-issue 12 --repo OtherOrg/other-repo
```

### What the Brief Contains

1. Task source (GitHub Issue metadata)
2. Full Issue body (preserved verbatim)
3. Project status snapshot (from `docs/project-status.md`)
4. Required reading list
5. Pre-operation Git verification steps
6. Objective
7. Allowed scope
8. Prohibited actions
9. Implementation requirements
10. Test and quality check commands
11. Post-completion Git verification
12. Final report format

### Closed Issues

The script refuses to generate a brief for a closed Issue. Only OPEN issues are
valid task sources. If you need to reference a closed Issue, copy its content
manually.

## Reviewing the Brief

Before handing the brief to Codex:

1. Read the entire output.
2. Verify the Issue title, body, and labels match your expectations.
3. Check the project status snapshot is current.
4. Confirm the required reading list is complete.

## Using the Brief with Codex

The generated brief is plain text. You can copy it to Codex in any of these ways:

### Option A: Paste into Codex interactive session

Start Codex and paste the brief as the initial prompt.

### Option B: Pipe from file

```bash
./scripts/codex-issue 12 --output /tmp/brief.txt
codex exec -C /path/to/repo -s workspace-write - < /tmp/brief.txt
```

### Option C: Pipe directly

```bash
./scripts/codex-issue 12 | codex exec -C /path/to/repo -s workspace-write -
```

## Why Codex Must Still Independently Verify

The brief includes project context and Issue content, but it is a snapshot.
Codex is instructed to:

- Independently verify the actual state of Git, the repository, docs, and GitHub.
- Stop and report discrepancies if the actual state conflicts.
- Never execute stash, reset, clean, merge, or rebase automatically.

## Isolating Work from a Dirty Workspace

If you have uncommitted changes in your main worktree, use Git Worktree:

```bash
# Create an isolated worktree from main
cd /path/to/main-repo
git fetch origin --prune
git worktree add -b codex/my-feature /path/to/new-worktree origin/main

# Work in the new worktree
cd /path/to/new-worktree
./scripts/codex-issue 12 | codex exec -C . -s workspace-write -
```

The original worktree and its uncommitted changes are untouched.

## High-Risk Git Commands

These commands should never be used carelessly:

- `git stash` — can lose context
- `git reset --hard` — destructive to working tree
- `git clean -fd` — deletes untracked files
- `git checkout -- <file>` — discards changes
- `git rebase` / `git merge` — can rewrite history

Codex is explicitly instructed not to execute these unless the task explicitly
requires them.

## Post-Completion Quality Checks

After implementation, run these checks:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
pytest
ruff check .
ruff format --check .
mypy .
```

For shell scripts:

```bash
bash -n scripts/<script>
```

## Checking the Diff

```bash
git status --short --branch
git diff --stat
git diff --check
git diff
```

Verify:
- Only expected files were modified.
- No migrations or unrelated code appeared.
- No secrets or local paths leaked into content.

## Generating a Completion Report

Follow the report format from AGENTS.md:

1. Completed task (roadmap stage or explicit task name)
2. Files created or modified
3. Verification commands and results
4. Data migration, deployment, or compatibility impact
5. Requirement conflicts or pending decisions
6. Work not completed or intentionally excluded
7. Recommended next stage (do not auto-start)
8. Final git status and diff summary

## Safety Checkpoints (Human Gates)

The workflow reserves three human approval points. The `codex-issue` script
does not automate any of these:

1. **After local development**: Review and approve `git commit` / `git push`.
2. **After CI passes**: Review and approve PR ready state.
3. **After final review**: Approve merge.

## Commit Message Guidelines

Use the repository's `.gitmessage` file as the default structure for important
commits. A one-line summary is enough for trivial changes, but anything that
touches behavior, architecture, or non-obvious design choices should include a
body.

Keep the Conventional Commit style summary line:

```text
<type>(<scope>): <summary>
```

When writing the body, prefer these sections in this order:

- **Why** — default for important commits. Explain the problem this change
  solves or the motivation behind it.
- **Decision** — include only when the implementation or design choice is not
  obvious from the diff alone.
- **Alternatives considered** — include only when real alternatives were
  actually evaluated. Do not fabricate options to fill the template.
- **Refs** — optional pointer to a GitHub Issue, PR, or ADR.

Rules of thumb:

- Do not invent reasons, alternatives, issue numbers, PR numbers, or ADR
  references that do not exist.
- Do not force a trivial typo fix, formatting change, or minor test addition
  to carry a full Decision or Alternatives section.
- The goal is to help future maintainers — and future AI/Codex instances —
  understand intent, not to add ceremonial text.

To install the template for this repository only, run:

```bash
git config commit.template .gitmessage
```

This is never done automatically; set it only when you want the template to
appear in your editor on every `git commit`.

## Token Efficiency Guidelines

Codex should load the minimum sufficient context for the current task. The
goal is to reduce redundant, unrelated, or repeated context — not to skip
necessary checks or code understanding.

### Core principle: progressive context expansion

Start with the smallest scope that is likely to be enough, and expand only
when the current scope is insufficient:

1. **Task context** — the current issue, brief, or user request.
2. **Module context** — files, services, and tests directly involved.
3. **Architecture context** — module boundaries, data flow, and relevant ADRs.
4. **Repository-wide context** — only for cross-module or high-risk changes.

### First-time project takeover

A new Codex session may read `AGENTS.md`, `README.md`, architecture docs,
relevant ADRs, the current stage/roadmap section, current Git state, and the
current PR state. Do not default to reading every ADR, every doc, or the
entire source tree.

### Continuing the same task or stage

If a file, ADR, service, or test has already been read and verified in the
current session and there is no reason to believe it changed, prefer targeted
updates over re-reading the whole file:

- `git diff` / `git diff --name-only` / `git diff --stat`
- `git show`
- targeted file reads
- `rg` / precise search

Re-expand the context only when:

- a file has actually changed,
- the implementation conflicts with known context,
- a test failure cannot be explained,
- a new dependency or cross-module impact is discovered,
- a high-risk operation requires re-verification.

### Code review

Use **diff-first review** by default:

1. Read changed files and the actual diff.
2. Locate modified functions and classes.
3. Read adjacent context and direct dependencies.
4. Expand to module or repository scope only when necessary.

For PR reviews, start with the `base...head` diff and expand from there.

### Testing

Prefer targeted tests during development:

- a single test,
- a single test file,
- the tests for the current app or service.

Run full validation at stage completion, PR submission, before merge, after
cross-module changes, or when AGENTS.md explicitly requires it.

Never skip a required quality gate to save tokens.

### Command output

Prefer concise output by default:

- `git status --short`
- `git diff --stat`
- targeted or quiet pytest runs

If tests pass, do not include large success logs in the analysis context. If
tests fail, start from the failing test and its traceback, expanding only when
diagnosis requires it.

Avoid repeatedly adding to context:

- very long `git log`
- full verbose pytest success logs
- full dependency trees
- full database dumps
- unrelated CI logs
- whole-repository file listings

### Git safety exceptions

Token efficiency must not weaken Git safety. Re-verify Git state before
high-risk operations such as creating or switching branches, committing,
rebasing, merging, resetting, cherry-picking, pushing, deleting branches, or
modifying worktrees. At minimum confirm the current branch, `git status`, and
HEAD.

### Documentation

Do not mechanically re-read the full README, AGENTS.md, architecture docs,
ADRs, or roadmap for every task. AGENTS.md mandates required pre-task reading
must still be followed; everything else is read based on relevance.

### Reports

Reports should be complete, structured, and focused on decisions and
exceptions. Avoid re-pasting command output, successful test logs, Git status,
or raw code that has already been shown. Summarize what was done, why, what
was found, verification results, risks, and next steps.

### Forbidden pseudo-optimizations

Do not skip reading actual code, relevant tests, Git safety checks,
migration checks, AGENTS.md, or relevant ADRs to reduce token usage. Do not
hide test failures or replace precise verification with summaries.

Reduce redundant context, not necessary context.

## Current Limitations (First Version)

The first version of `codex-issue` is read-only:

- It **does not** call Codex automatically.
- It **does not** create branches or worktrees.
- It **does not** commit, push, or create PRs.
- It **does not** modify GitHub Issues.
- It **does not** execute Issue body content.
- It outputs a text brief for you to review and use manually.

Automatic `--comment` feedback to GitHub Issues is planned but not yet
implemented.

## Shell Alias (Optional)

If you run `codex-issue` frequently, you may add an alias to your shell
configuration. This is never done automatically. Because the script is at a
repository-relative path, a shell function that locates the repo root, or an
absolute-path alias, is more robust than a bare `./scripts/` relative alias.
Example (adjust path for your machine):

```bash
# In ~/.zshrc or ~/.bashrc:
codex-issue() {
    local repo="/path/to/earnings-radar"
    "$repo/scripts/codex-issue" "$@"
}
```
