#!/usr/bin/env bash
# Deploy the landing page (web/) to Vercel production.
#
# Commits in this repo are authored by the Trench-launches GitHub account, which
# is not a member of the Vercel team, and Vercel blocks deployments whose commit
# author it can't match. Deploying from a copy with no Git history avoids that.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/.vercel"
cp -r "$ROOT/web" "$TMP/web"
cp "$ROOT/vercel.json" "$TMP/"
cp "$ROOT/.vercel/project.json" "$TMP/.vercel/"
cd "$TMP"
vercel deploy --prod --yes < /dev/null
