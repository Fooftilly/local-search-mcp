# Local-search-mcp agent instructions

Follow [CONTRIBUTING.md](CONTRIBUTING.md) for test commands, security
boundaries, and review expectations.

## Pull request coordination and Mergify

- Do not autonomously merge or queue pull requests. Merge a particular PR only
  after the maintainer explicitly authorizes it and after verifying current
  mergeability, required checks, and unresolved review findings.
- Do not enable auto-merge, Mergify Workflow Automation, Merge Queue, automatic
  retry, or automatic test quarantine. The scheduled flaky-test detection
  workflow is for reporting only, not for retrying or merging failing tests.
- Never use `@mergifyio queue` as a shortcut. The Mergify dashboard's Merge
  Queue setting must remain disabled; `.mergify.yml` cannot disable it.
- Use a separate `Depends-On: #<PR number>` line in the dependent PR body
  **only for a genuine PR-to-PR merge prerequisite**. For another repository
  with Mergify enabled under the same owner, use its full PR URL.
  Do not use `Depends-On:` for issues, loosely related work, or parallel PRs
  that can safely merge independently.
- When a dependency is declared, verify that Mergify recognizes it and that
  the prerequisite has actually merged before merging the dependent PR.
  Do not remove a valid dependency merely to make checks pass. Explain
  corrections to obsolete or incorrect dependency lines.
- Titles containing standalone `WIP`, `DNM`, or `DO NOT MERGE` mean the PR
  is not ready, even if the draft flag is cleared. Do not rename these titles
  just to bypass an intentionally unfinished state.
- The Mergify Merge Protections check only blocks GitHub's manual merge button
  after it is required in the active GitHub `master` branch ruleset. Until
  then, inspect dependencies and readiness manually.
