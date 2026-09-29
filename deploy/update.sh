#!/usr/bin/env bash
# Update the VM to the latest main: git pull, requirements (only if changed), frontend build,
# database migrations, systemd units, restart. Safe to re-run.
#
#   cd ~/JMW-Systematic && bash deploy/update.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# Everything runs inside main() so bash has parsed the whole file before `git pull` may replace it.
main() {
  require_normal_user
  cd "$REPO_DIR"

  if [ "${1:-}" != "--no-pull" ]; then
    before="$(git rev-parse HEAD)"
    log "Pulling the latest main"
    git pull --ff-only
    after="$(git rev-parse HEAD)"
    if [ "$before" = "$after" ]; then
      log "Already at $(git log -1 --format='%h %s')"
    else
      log "Updated $(git rev-parse --short "$before") -> $(git log -1 --format='%h %s')"
      if ! git diff --quiet "$before" "$after" -- deploy/; then
        log "Deploy scripts changed: continuing with the new version"
        exec bash "$REPO_DIR/deploy/update.sh" --no-pull
      fi
    fi
  fi

  chmod 600 .env
  install_requirements
  fetch_frontend

  wait_for_daily_cycle
  log "Stopping the backend for the migration"
  sudo systemctl stop jmw-backend.service
  run_migrations
  install_units
  sudo systemctl enable --quiet jmw-backend.service jmw-daily.timer jmw-backup.timer
  sudo systemctl start jmw-backend.service jmw-daily.timer jmw-backup.timer
  health_check
  systemctl list-timers 'jmw-*' --no-pager || true
}

main "$@"
exit $?
