#!/usr/bin/env bash
# Applies the GitHub settings this fork's push rules and CI rely on.
# Needs an org owner's gh login with the repo, admin:org and workflow scopes.
# Safe to re-run: the team, rulesets and environment are updated in place.
#
#   1. px4-maintainers team: bypasses the rulesets; its maintain role is what
#      the !ai-review command checks.
#   2. Rulesets: every branch and tag is maintainers only, whatever its name.
#      Other members work in their own forks and open PRs here.
#   3. ai-review environment: its secrets reach only workflows on agam-ci.
#   4. Upstream workflows that need PX4's own runners or accounts are
#      disabled; otherwise they sit queued on every PR here.
#   5. agam-ci becomes the default branch, so pull_request_target and
#      issue_comment workflows run from it.
set -euo pipefail

ORG=agam-robotics
REPO=$ORG/PX4-Autopilot
TEAM=px4-maintainers
MAINTAINERS=(abhijithcv kapilaare Karthik-Ranga avinashkrg90)
# upstream workflows that work on GitHub-hosted runners and stay enabled
KEEP=(.github/workflows/commit_checks.yml
	.github/workflows/usb_ids.yml
	.github/workflows/sbom_license_check.yml)
dir=$(cd "$(dirname "$0")" && pwd)

echo "== team $TEAM"
if ! gh api "orgs/$ORG/teams/$TEAM" >/dev/null 2>&1; then
	gh api "orgs/$ORG/teams" -f name="$TEAM" -f privacy=closed \
		-f description="Can push, merge and tag in PX4-Autopilot" >/dev/null
fi
for user in "${MAINTAINERS[@]}"; do
	gh api -X PUT "orgs/$ORG/teams/$TEAM/memberships/$user" -f role=member \
		--jq '"  \(.url | split("/") | last): \(.state)"'
done
gh api -X PUT "orgs/$ORG/teams/$TEAM/repos/$REPO" -f permission=maintain
team_id=$(gh api "orgs/$ORG/teams/$TEAM" --jq .id)

echo "== rulesets"
for file in "$dir"/ruleset-*.json; do
	name=$(jq -r .name "$file")
	body=$(jq --argjson id "$team_id" \
		'.bypass_actors = [{"actor_id": $id, "actor_type": "Team", "bypass_mode": "always"}]' "$file")
	id=$(gh api "repos/$REPO/rulesets" --jq ".[] | select(.name == \"$name\") | .id")
	if [ -n "$id" ]; then
		gh api -X PUT "repos/$REPO/rulesets/$id" --input - <<<"$body" >/dev/null
	else
		gh api -X POST "repos/$REPO/rulesets" --input - <<<"$body" >/dev/null
	fi
	echo "  $name"
done

echo "== environment ai-review"
gh api -X PUT "repos/$REPO/environments/ai-review" --input - >/dev/null <<<'
	{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}'
if ! gh api "repos/$REPO/environments/ai-review/deployment-branch-policies" \
	--jq '.branch_policies[].name' | grep -qx agam-ci; then
	gh api -X POST "repos/$REPO/environments/ai-review/deployment-branch-policies" \
		-f name=agam-ci -f type=branch >/dev/null
fi
echo "  deployments allowed from: agam-ci"

echo "== upstream workflows"
# GitHub lists only the default branch's workflows and can disable only
# listed ones, so list them with main as the default for a moment. The
# disabled state survives the switch back, and a re-run catches workflows
# PX4 adds later.
gh api -X PATCH "repos/$REPO" -f default_branch=main >/dev/null
gh api "repos/$REPO/actions/workflows" --paginate \
	--jq '.workflows[] | select(.state == "active") | "\(.id) \(.path)"' |
	while read -r id path; do
		case "$path" in
		.github/workflows/agam-*) continue ;;
		.github/workflows/*) ;;
		*) continue ;;  # GitHub's own dynamic workflows
		esac
		case " ${KEEP[*]} " in *" $path "*) continue ;; esac
		gh api -X PUT "repos/$REPO/actions/workflows/$id/disable" </dev/null
		echo "  disabled $path"
	done

echo "== default branch"
gh api -X PATCH "repos/$REPO" -f default_branch=agam-ci --jq '"  \(.default_branch)"'
