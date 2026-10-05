#!/usr/bin/env bash
# Publish the dashboard to GitHub Pages.
#
#   ./deploy.sh              code + website in one public repo (site served from /docs)
#   ./deploy.sh --site-only  website only, in its own public repo (no source code)
#
# First time: run `gh auth login` once. Re-run this script after each data refresh.
set -euo pipefail
cd "$(dirname "$0")"
REPO="${REPO:-idx-foreign-ownership}"
PY=.venv/bin/python

gh auth status >/dev/null 2>&1 || { echo "Run 'gh auth login' first."; exit 1; }
OWNER=$(gh api user --jq .login)
gh auth setup-git >/dev/null            # let git push with the gh login
git config --global http.postBuffer >/dev/null || git config --global http.postBuffer 157286400  # avoid HTTP 400 on larger pushes
OWNER_ID=$(gh api user --jq .id)
OWNER_NAME=$(gh api user --jq '.name // .login')

# Commit as GitHub's private no-reply address so no personal email is published.
use_private_identity() {
  git config user.email >/dev/null || git config user.email "$OWNER_ID+$OWNER@users.noreply.github.com"
  git config user.name  >/dev/null || git config user.name "$OWNER_NAME"
}
COMMIT_TRAILER=$'\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>'

enable_pages() {  # $1 = path ("/docs" or "/")
  gh api "repos/$OWNER/$REPO/pages" >/dev/null 2>&1 && return 0
  gh api -X POST "repos/$OWNER/$REPO/pages" -f "source[branch]=main" -f "source[path]=$1" >/dev/null
}

if [[ "${1:-}" == "--site-only" ]]; then
  OUT=$(mktemp -d)/site
  $PY -m web.build_static --out "$OUT"
  touch "$OUT/.nojekyll"
  cd "$OUT"
  git init -q -b main && use_private_identity
  git add -A && git commit -qm "Publish IDX foreign ownership dashboard$COMMIT_TRAILER"
  if gh repo view "$OWNER/$REPO" >/dev/null 2>&1; then
    git remote add origin "https://github.com/$OWNER/$REPO.git" && git push -qf origin main
  else
    gh repo create "$REPO" --public --source=. --push --description "Foreign institutional ownership of Indonesian equities, 2020-2026"
  fi
  enable_pages "/"
else
  rm -rf docs && $PY -m web.build_static --out docs && touch docs/.nojekyll
  [[ -d .git ]] || git init -q -b main
  use_private_identity
  git add -A && git commit -qm "Update dashboard ($(date +%Y-%m-%d))$COMMIT_TRAILER" || echo "Nothing new to commit."
  if git remote get-url origin >/dev/null 2>&1; then
    git push -q origin main
  else
    gh repo create "$REPO" --public --source=. --push --description "Foreign institutional ownership of Indonesian equities, 2020-2026"
  fi
  enable_pages "/docs"
fi
echo
echo "Code:    https://github.com/$OWNER/$REPO"
echo "Website: https://$OWNER.github.io/$REPO/  (first publish can take 1-3 minutes)"
echo "Commits use $(git config user.email)"
