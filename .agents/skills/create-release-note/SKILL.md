---
name: create-release-note
description: "Create a release note change artifact YAML file for a merged or open PR. Use when: writing release notes, creating change artifacts, documenting PR changes, adding release note entries."
argument-hint: "PR number or URL. If omitted, uses active PR or git history."
---

# Create Release Note Artifact

## When to Use

- A PR has been merged or is ready to merge and needs a change artifact for the release notes.
- The user asks to create, write, or add a release note for a PR.

## Procedure

### 1. Identify the PR

Use the first available source: user-provided PR number/URL → active PR from repository context → no PR (rely on git history in step 2).

### 2. Analyze Git History

Always run `git log --oneline main..HEAD` (or the appropriate default branch) to understand the changes on the current branch. This provides ground truth for the artifact regardless of whether a PR exists.

Review the commit messages and, if needed, run `git diff main..HEAD --stat` to understand the scope of changes.

### 3. Gather PR Details (if available)

Attempt to load `github-pull-request_issue_fetch` via `tool_search`. If the tool loads successfully, use it to fetch:

- Title
- Author (GitHub username, no `@` prefix)
- Description/body (to understand the change)
- PR URL

Cross-reference the PR description against the git history from step 2. If the PR description is incomplete or does not cover all commits, use the git history as the authoritative source for the artifact content.

If no PR exists yet or `tool_search` fails, rely entirely on the git history analysis from step 2. Derive the author from `git log` output.

### 4. Determine Artifact Values

Based on the git history and PR content (if available), determine:

| Field                | How to decide                                                                                         |
| -------------------- | ----------------------------------------------------------------------------------------------------- |
| `title`              | Short, past-tense summary of what changed. Keep concise.                                              |
| `author`             | GitHub username (no `@`, no email)                                                                    |
| `type`               | `major` (immediately visible to users), `minor` (less visible), `bugfix`, `breaking`, or `deprecated` |
| `description`        | Past-tense explanation of how the change impacts users. Provide context.                              |
| `urls.pr`            | Array of PR URLs (one or more)                                                                        |
| `urls.related_doc`   | Link to public documentation if applicable                                                            |
| `urls.related_issue` | Link to GitHub issue if applicable                                                                    |
| `visibility`         | `public` (user-facing), `internal` (infra/CI/deps), `hidden` (docs-only)                              |
| `highlight`          | `true` if: deprecated, or major/bugfix with significant user impact                                   |

**Visibility guidelines:**

- Documentation-only PRs → `hidden`
- Dependency updates, CI changes, internal refactors → `internal`
- User-facing features, config changes, bug fixes → `public`

**Type guidelines:**

- Will users immediately see or feel the change? → `major`
- Less likely to affect user experience meaningfully? → `minor`
- Fixes a bug? → `bugfix`
- Backwards-incompatible? → `breaking`
- Removes or deprecates a feature? → `deprecated`

### 5. Create the Artifact File

- **Filename**: `pr{NUMBER:04d}.yaml` (e.g., `pr0118.yaml` for PR #118)
- **Location**: `docs/release-notes/artifacts/`
- **Schema version**: `2`

Use the template at `docs/release-notes/template/_change-artifact-template.yaml` as the base structure. Refer to existing artifacts in `docs/release-notes/artifacts/` for real examples of how fields are filled in.

### 6. Confirm with User

Present the artifact content. Only ask for confirmation on fields where confidence is low (e.g., ambiguous `type` between major/minor, unclear `visibility`, or uncertain `highlight`). If the change is straightforward and all fields are obvious from the git history and PR context, save the file without asking.

## Style Rules

- **title**: Past tense, short, specific (e.g., "Added S3 backup support", "Fixed login redirect loop")
- **description**: Past tense, user-focused. Describe impact, not implementation details.
- **author**: GitHub username only — no `@`, no email addresses.
- Multiple related PRs for one feature → single artifact with multiple `urls.pr` entries.
