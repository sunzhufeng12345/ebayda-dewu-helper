#!/usr/bin/env bash
set -euo pipefail

: "${STAGING_SSH:?Set STAGING_SSH to the SSH alias or user@host}"
: "${STAGING_APP_DIR:?Set STAGING_APP_DIR to the checked-out BPMS directory}"
: "${STAGING_REF:?Set STAGING_REF to a branch, tag, or commit}"

STAGING_SERVICE="${STAGING_SERVICE:-}"
STAGING_APPLY="${STAGING_APPLY:-0}"

printf 'staging host: %s\n' "$STAGING_SSH"
printf 'application directory: %s\n' "$STAGING_APP_DIR"
printf 'requested ref: %s\n' "$STAGING_REF"
printf 'service: %s\n' "${STAGING_SERVICE:-<not restarted>}"

if [[ "$STAGING_APPLY" != "1" ]]; then
  printf 'dry run only; set STAGING_APPLY=1 to deploy\n'
  exit 0
fi

ssh "$STAGING_SSH" bash -s -- \
  "$STAGING_APP_DIR" "$STAGING_REF" "$STAGING_SERVICE" <<'REMOTE'
set -euo pipefail

app_dir=$1
requested_ref=$2
service_name=$3

cd "$app_dir"
if [[ -n "$(git status --porcelain)" ]]; then
  printf 'refusing deployment: staging worktree is dirty\n' >&2
  git status --short >&2
  exit 1
fi

git fetch origin --prune
git cat-file -e "${requested_ref}^{commit}"
git checkout --detach "$requested_ref"

if [[ -n "$service_name" ]]; then
  sudo systemctl restart "$service_name"
fi

printf 'deployed commit: '
git rev-parse HEAD
REMOTE
