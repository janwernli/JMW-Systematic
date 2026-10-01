#!/usr/bin/env bash
# Shared helpers for deploy/setup.sh and deploy/update.sh (sourced, not run directly).

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
RUN_HOME="$HOME"
VENV="$REPO_DIR/backend/.venv"
PY="$VENV/bin/python"
PORT=8765
UNITS=(jmw-backend.service jmw-daily.service jmw-daily.timer jmw-backup.service jmw-backup.timer)

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

require_normal_user() {
  if [ "$(id -u)" -eq 0 ]; then
    die "run this as your normal user (e.g. azureuser), not as root; it calls sudo itself where needed"
  fi
  sudo -n true 2>/dev/null || sudo -v || die "this script needs sudo rights"
}

# owner/repo from the git remote (https://github.com/o/r.git or git@github.com:o/r.git)
repo_slug() {
  git -C "$REPO_DIR" remote get-url origin | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##'
}

# Install backend requirements into the venv only when requirements.txt changed (or the venv is new).
install_requirements() {
  local req="$REPO_DIR/backend/requirements.txt" stamp="$VENV/.requirements.sha256" want
  if [ ! -x "$PY" ]; then
    log "Creating Python 3.12 virtualenv in backend/.venv"
    python3.12 -m venv "$VENV"
  fi
  want="$(sha256sum "$req" | cut -d' ' -f1)"
  if [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$want" ]; then
    log "Backend requirements unchanged"
    return
  fi
  log "Installing backend requirements"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet --no-cache-dir -r "$req"
  echo "$want" > "$stamp"
}

# Read-only GitHub token for release downloads from a PRIVATE repo (GITHUB_RELEASE_TOKEN in .env; the app
# itself never reads it). Empty for a public repo.
release_token() {
  sed -n 's/^GITHUB_RELEASE_TOKEN=//p' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | tr -d '"'"'"' \r'
}

# Download one asset of the frontend-latest release to $2: via the API with a token (private repo),
# otherwise from the public download URL.
download_asset() {
  local slug="$1" name="$2" out="$3" token id
  token="$(release_token)"
  if [ -z "$token" ]; then
    curl -fsSL --retry 3 -o "$out" "https://github.com/$slug/releases/download/frontend-latest/$name"
    return
  fi
  id="$(curl -fsSL --retry 3 -H "Authorization: Bearer $token" -H "Accept: application/vnd.github+json" \
          "https://api.github.com/repos/$slug/releases/tags/frontend-latest" \
        | python3 -c 'import json,sys; n=sys.argv[1]; print(next(a["id"] for a in json.load(sys.stdin)["assets"] if a["name"]==n))' "$name")" \
    || return 1
  curl -fsSL --retry 3 -H "Authorization: Bearer $token" -H "Accept: application/octet-stream" -o "$out" \
    "https://api.github.com/repos/$slug/releases/assets/$id"
}

# Download the frontend build published by .github/workflows/frontend.yml and swap it in atomically.
fetch_frontend() {
  local slug tmp local_tree built_tree
  slug="$(repo_slug)"
  tmp="$(mktemp -d)"
  log "Downloading the frontend build from github.com/$slug (release frontend-latest$([ -n "$(release_token)" ] && echo ', with token'))"
  if ! download_asset "$slug" frontend-dist.tar.gz "$tmp/frontend-dist.tar.gz" \
     || ! download_asset "$slug" frontend-dist.tar.gz.sha256 "$tmp/frontend-dist.tar.gz.sha256"; then
    rm -rf "$tmp"
    warn "could not download the frontend build. Either the 'Frontend build' GitHub Action has not run on main yet, or the repo is private and GITHUB_RELEASE_TOKEN in .env is missing, wrong or expired. The API keeps working; the dashboard page stays at the previous build (or missing)."
    return 0
  fi
  (cd "$tmp" && sha256sum --quiet -c frontend-dist.tar.gz.sha256) || die "frontend build checksum mismatch"
  tar -xzf "$tmp/frontend-dist.tar.gz" -C "$tmp"
  [ -f "$tmp/dist/index.html" ] || die "frontend build is missing index.html"
  built_tree="$(sed -n 's/^frontend_tree=//p' "$tmp/dist/BUILD_INFO" 2>/dev/null || true)"
  local_tree="$(git -C "$REPO_DIR" rev-parse HEAD:frontend)"
  if [ -n "$built_tree" ] && [ "$built_tree" != "$local_tree" ]; then
    warn "the published frontend build is for a different frontend version than this checkout (the GitHub Action may still be running). Installing it anyway; re-run deploy/update.sh in a few minutes."
  fi
  rm -rf "$REPO_DIR/frontend/dist.old"
  [ -d "$REPO_DIR/frontend/dist" ] && mv "$REPO_DIR/frontend/dist" "$REPO_DIR/frontend/dist.old"
  mv "$tmp/dist" "$REPO_DIR/frontend/dist"
  rm -rf "$REPO_DIR/frontend/dist.old" "$tmp"
  log "Frontend installed ($(sed -n 's/^commit=//p' "$REPO_DIR/frontend/dist/BUILD_INFO" 2>/dev/null | cut -c1-8))"
}

run_migrations() {
  log "Applying database migrations"
  (cd "$REPO_DIR/backend" && "$PY" -m app migrate)
}

# Render the unit templates (fill in paths and user) into /etc/systemd/system.
install_units() {
  local u changed=0 tmp
  for u in "${UNITS[@]}"; do
    tmp="$(mktemp)"
    sed -e "s#@REPO@#$REPO_DIR#g" -e "s#@USER@#$RUN_USER#g" -e "s#@HOME@#$RUN_HOME#g" \
      "$REPO_DIR/deploy/systemd/$u" > "$tmp"
    if ! sudo cmp -s "$tmp" "/etc/systemd/system/$u"; then
      sudo install -m 0644 "$tmp" "/etc/systemd/system/$u"
      changed=1
    fi
    rm -f "$tmp"
  done
  if [ "$changed" = 1 ]; then
    log "systemd units updated"
    sudo systemctl daemon-reload
  fi
}

# Do not restart or migrate underneath a running daily cycle.
wait_for_daily_cycle() {
  local i
  for i in $(seq 1 90); do
    systemctl is-active --quiet jmw-daily.service || return 0
    [ "$i" = 1 ] && log "Waiting for the running daily cycle to finish"
    sleep 10
  done
  die "the daily cycle is still running after 15 minutes; try again later"
}

health_check() {
  local i
  for i in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$PORT/api/health" > /dev/null 2>&1; then
      log "Backend is healthy on http://127.0.0.1:$PORT"
      return 0
    fi
    sleep 2
  done
  warn "backend did not answer on 127.0.0.1:$PORT; see: journalctl -u jmw-backend -n 100"
  return 1
}
