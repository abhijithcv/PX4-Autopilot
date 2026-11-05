#!/usr/bin/env bash
# Blobless fetch of a PR head (as refs/pr/head) and its base branch (as
# origin/<base>), for reading with git only: no checkout, nothing executed.
# File contents download on demand.
#
# usage: fetch_pr.sh <dir> <base-branch> <pr-number>
set -euo pipefail

dir=$1
base=$2
pr=$3

git init -q "$dir"
git -C "$dir" remote add origin "https://github.com/${GITHUB_REPOSITORY}.git"
git -C "$dir" config remote.origin.promisor true
git -C "$dir" config remote.origin.partialclonefilter blob:none
git -C "$dir" fetch -q --no-tags --filter=blob:none origin \
	"+refs/heads/${base}:refs/remotes/origin/${base}" \
	"+refs/pull/${pr}/head:refs/pr/head"
