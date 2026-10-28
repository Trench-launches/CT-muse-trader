#!/usr/bin/env bash
# Deploy the landing page (web/) to Vercel production.
#
# The site's per-deployment settings come from SITE_* variables (set them in your shell, or put them in a
# gitignored .env.site file at the repo root) and are written into web/config.js in the deployed copy only:
#   SITE_API_URL      required: the backend's public URL, e.g. https://api.example.com
#   SITE_API_FALLBACK optional: an older backend URL, tried when the first can't be reached
#   SITE_NAME         optional: the persona's name (default VIGIL)
#   SITE_X_HANDLE     optional: the bot's X handle, without the @
#   SITE_TOKEN_CA     optional: the project's own token contract, once launched
#   SITE_TOKEN_GMGN   optional: its GMGN link
# Only public values belong here: everything in config.js is served to every visitor.
#
# Vercel blocks deployments whose Git commit author isn't a member of the Vercel team. Deploying from a copy
# with no Git history avoids that. Needs a linked project (.vercel/project.json, from `vercel link`).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$ROOT/.env.site" ]; then
  set -a; . "$ROOT/.env.site"; set +a
fi
if [ -z "${SITE_API_URL:-}" ]; then
  echo "SITE_API_URL is not set: the site wouldn't know where its backend is. See the top of $0." >&2
  exit 1
fi
[ -n "${SITE_X_HANDLE:-}" ] || echo "note: SITE_X_HANDLE is not set; the site won't link to an X account" >&2
PY="$(command -v python3 || command -v python)"
TMP="$(mktemp -d)"
trap 'cd "$ROOT"; rm -rf "$TMP" 2>/dev/null || true' EXIT  # Windows may still hold the folder briefly; leftover temp files are harmless
mkdir -p "$TMP/.vercel"
cp -r "$ROOT/web" "$TMP/web"
cp "$ROOT/vercel.json" "$TMP/"
cp "$ROOT/.vercel/project.json" "$TMP/.vercel/"
# json.dumps escapes every value, so nothing in a variable can break out of the file
"$PY" - "$TMP/web/config.js" <<'EOF'
import json, os, re, sys
env = os.environ.get
handle = env("SITE_X_HANDLE", "").lstrip("@")
if handle and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
    sys.exit(f"SITE_X_HANDLE {handle!r} isn't an X handle")
for k in ("SITE_API_URL", "SITE_API_FALLBACK", "SITE_TOKEN_GMGN"):
    if env(k) and not env(k).startswith("https://"):
        sys.exit(f"{k} must be an https:// URL")
cfg = {"name": env("SITE_NAME") or "VIGIL", "xHandle": handle, "api": env("SITE_API_URL"),
       "apiFallback": env("SITE_API_FALLBACK", ""),
       "token": {"ca": env("SITE_TOKEN_CA", ""), "gmgn": env("SITE_TOKEN_GMGN", "")}}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    f.write("// Written by scripts/deploy-web.sh at deploy time.\nwindow.SITE_CONFIG = " + json.dumps(cfg) + ";\n")
print("site config:", json.dumps(cfg))
EOF
cd "$TMP"
vercel deploy --prod --yes < /dev/null
