# Agam Robotics' PX4 fork

This is Agam's fork of [PX4/PX4-Autopilot](https://github.com/PX4/PX4-Autopilot).
**The PX4 source is on [`main`](../../tree/main)**, a plain mirror of upstream.
You are looking at `agam-ci`, the default branch, which only holds this guide
and the fork's CI.

## Who can do what

Access depends only on your role, never on a branch name:

- **The `px4-maintainers` team** can push, merge, create and delete any branch
  or tag here.
- **Everyone else** works in their own fork and opens a pull request to this
  repository. A maintainer reviews it, then merges it here or raises it to PX4.

A pull request whose branch name starts with `pr-` is reviewed automatically:
format and hygiene checks, builds, and an AI review at medium effort. That is
all the prefix does.

## Making a change (for interns)

**1. One-time setup.** Install [git](https://git-scm.com/downloads) and the
[GitHub CLI](https://cli.github.com/), then:

```sh
gh auth login
git config --global user.name  "Your Name"
git config --global user.email "the-email-on-your-github-account@example.com"
git clone --recursive -b main -o upstream https://github.com/agam-robotics/PX4-Autopilot.git
cd PX4-Autopilot
gh repo fork --remote --remote-name origin
gh repo set-default agam-robotics/PX4-Autopilot
```

You now have two remotes. `upstream` is this repository, where your changes
start and where pull requests go. `origin` is your own fork, where you push. If
you already have a fork of PX4, `gh repo fork` reuses it.

**2. Start every change from the latest `main`**, on a new branch whose name
starts with `pr-` and says what it is:

```sh
git fetch upstream
git checkout -b pr-ina228-shunt-fix upstream/main
git submodule update --init --recursive
```

**3. Commit** with a message in PX4's format, `type(scope): what changed`:

```sh
git status                 # check that only your files are listed
git add src/drivers/power_monitor/ina228/ina228.cpp
git commit -m "fix(drivers/ina228): correct the shunt scaling"
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`,
`ci`, `chore`, `revert`. Add files by name. Avoid `git add .` and `git add -A`:
they pick up build output, logs and submodule changes.

**4. Push to your fork, then open the pull request here:**

```sh
git push -u origin pr-ina228-shunt-fix
gh pr create --repo agam-robotics/PX4-Autopilot --base main \
  --head "$(gh api user --jq .login):pr-ina228-shunt-fix" --fill
```

> [!WARNING]
> Never open a pull request on **PX4/PX4-Autopilot** yourself. GitHub's
> "Compare & pull request" button picks PX4 by default. If you use the web
> page, open
> `https://github.com/agam-robotics/PX4-Autopilot/compare/main...YOUR-USERNAME:pr-YOUR-BRANCH?expand=1`
> instead, and check that the page says `agam-robotics/PX4-Autopilot` next to
> "base repository".

**5. Read the checks.** Within a few minutes the PR shows:

- **Agam PR checks**: a comment listing anything to fix, each with the commands
  that fix it. Typical problems: an accidental submodule update, a commit
  message in the wrong format, a stray build file, wrong board IDs.
- **PX4 gate**: PX4's own format checks. If `check_format` fails, its log
  shows the exact change. Make it by hand, or let PX4's container do it
  (needs Docker): `./Tools/docker_run.sh 'make format'`. Then commit and push.
- **Build**: the firmware your change touches.
- **AI review**: comments from Claude on the code itself.

**6. Fix and push again** to the same branch. Every push re-runs everything
and updates the comment. When the checks are green, ask a maintainer to review.

If you get stuck with git, stop and ask. Don't use `--force` without
`--with-lease`, and don't delete or recreate the branch.

## For maintainers

**On-demand AI review:** comment on the PR with only one of

```text
!ai-review
!ai-review effort=medium
!ai-review model=sonnet effort=high
```

Models are `opus` and `sonnet`; efforts are `low`, `medium`, `high`, `xhigh`
and `max`. With no arguments it uses PX4's defaults (Opus, `xhigh`). Only the
`maintain` and `admin` roles can trigger it. The review is PX4's own pipeline
(`Tools/ci/ai_review` on `main`), posted as a comment review with its cost.

**Automatic review:** every push to a `pr-*` PR from someone with write access
gets the same review as `!ai-review effort=medium`. The repository variables
`AGAM_AUTO_REVIEW` (`off`), `AGAM_AUTO_REVIEW_MODEL` and
`AGAM_AUTO_REVIEW_EFFORT` change that.

**Changing who can push:** edit the `px4-maintainers` team. It is the rulesets'
only bypass, and its `maintain` role gates `!ai-review`.

**Keeping `main` current:** "Sync fork" on `main` as before. `agam-ci`
shares no history with PX4, so never sync or rebase it onto upstream.

| File | What it does |
|---|---|
| `.github/workflows/agam-pr-checks.yml` | checks every `pr-*` PR |
| `.github/workflows/agam-ai-review.yml` | the `!ai-review` command |
| `.github/workflows/agam-ai-review-run.yml` | runs PX4's AI review with the Anthropic API |
| `.github/agam/pr_checks.py` | hygiene, commit and board-ID checks, build targets |
| `.github/agam/bin/claude` | points PX4's Bedrock-only review at the Anthropic API |
| `.github/agam/settings/` | the team, rulesets and other repository settings (`apply.sh`) |

Tests: `cd .github/agam && python3 -m unittest test_pr_checks`.
