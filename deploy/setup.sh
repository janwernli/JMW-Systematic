#!/usr/bin/env bash
# One-time (and safe to re-run) setup of JMW Capital Partners Trading on an Ubuntu 24.04 VM.
#
#   git clone https://github.com/janwernli/JMW-Systematic.git ~/JMW-Systematic
#   cd ~/JMW-Systematic && bash deploy/setup.sh
#
# Run as your normal user (azureuser); the script uses sudo where needed. Every step is idempotent.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# Everything runs inside main() so bash has parsed the whole file before `git pull` may replace it.
main() {
  require_normal_user
  cd "$REPO_DIR"

  # ---------------------------------------------------------------- packages
  log "Installing system packages (Python 3.12, venv, sqlite3, ufw, unattended-upgrades)"
  export DEBIAN_FRONTEND=noninteractive
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3.12 python3.12-venv python3-pip sqlite3 curl ca-certificates ufw \
    unattended-upgrades tzdata > /dev/null

  # ---------------------------------------------------------------- swap (1 GiB RAM needs it)
  if [ -z "$(swapon --show --noheadings)" ]; then
    log "No swap found: creating a 2 GB /swapfile"
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile > /dev/null
    sudo swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
  else
    log "Swap present: $(swapon --show --noheadings | awk '{print $1" "$3}' | paste -sd' ')"
  fi

  # ---------------------------------------------------------------- .env
  if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    warn ".env created from .env.example. Edit it now (Alpaca PAPER keys, SEC_USER_AGENT, NTFY_TOPIC):"
    warn "    nano $REPO_DIR/.env"
    warn "then run this script again."
    exit 1
  fi
  chmod 600 .env
  grep -q '^ALPACA_API_KEY_ID=..*' .env || warn ".env has no ALPACA_API_KEY_ID: data refresh and the daily cycle will fail"
  grep -q '^BROKER_TRADING_ENABLED=true' .env \
    || log "BROKER_TRADING_ENABLED is not true: the daily cycle computes orders but never sends them"

  mkdir -p data logs "$RUN_HOME/backups"
  chmod 700 data "$RUN_HOME/backups"

  # ---------------------------------------------------------------- app
  install_requirements
  fetch_frontend
  wait_for_daily_cycle
  run_migrations

  # ---------------------------------------------------------------- systemd
  install_units
  sudo systemctl enable --quiet jmw-backend.service jmw-daily.timer jmw-backup.timer
  sudo systemctl restart jmw-backend.service
  sudo systemctl start jmw-daily.timer jmw-backup.timer
  health_check || true

  # ---------------------------------------------------------------- unattended security upgrades
  log "Enabling unattended-upgrades (security updates; reboot if required at 04:00 UTC)"
  sudo tee /etc/apt/apt.conf.d/20auto-upgrades > /dev/null <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
  # 04:00 UTC = 23:00/00:00 New York: no scheduled run nearby; services and timers come back by themselves.
  sudo tee /etc/apt/apt.conf.d/52jmw-unattended-upgrades > /dev/null <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
EOF
  sudo systemctl enable --quiet --now unattended-upgrades.service

  # ---------------------------------------------------------------- firewall
  log "Configuring ufw: deny incoming except OpenSSH and the tailscale0 interface"
  sudo ufw default deny incoming > /dev/null
  sudo ufw default allow outgoing > /dev/null
  sudo ufw allow OpenSSH > /dev/null
  sudo ufw allow in on tailscale0 > /dev/null
  sudo ufw --force enable > /dev/null
  sudo ufw status verbose | sed -n '1,20p'

  # ---------------------------------------------------------------- HTTPS on the tailnet
  if command -v tailscale > /dev/null 2>&1 \
     && [ "$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("BackendState",""))' 2>/dev/null)" = "Running" ]; then
    log "Publishing the dashboard on your tailnet with tailscale serve (persists across reboots)"
    sudo tailscale serve --bg "$PORT" > /dev/null
    sudo tailscale serve status || true
    dns="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
    log "Dashboard: https://$dns"
  else
    warn "Tailscale is not installed or not logged in, so the dashboard is only reachable on the VM itself."
    warn "  curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up"
    warn "  (enable MagicDNS + HTTPS certificates in the Tailscale admin console), then re-run deploy/setup.sh"
  fi

  # ---------------------------------------------------------------- summary
  log "Timers:"
  systemctl list-timers 'jmw-*' --no-pager || true
  cat <<EOF

  Done. Useful commands:
    systemctl list-timers 'jmw-*'          next scheduled runs (08:00 / 17:30 New York, weekdays; backup 21:00)
    journalctl -u jmw-backend -f           API / dashboard log
    journalctl -u jmw-daily -n 200         daily cycle log
    sudo systemctl start jmw-daily         run the daily cycle now
    bash deploy/update.sh                  pull, install, migrate, restart

  IMPORTANT: run the scheduler in ONE place only. If this VM trades the Alpaca paper account, remove the
  Windows tasks on your PC (npm run schedule:remove), otherwise both would place orders for the same account.
EOF
}

main "$@"
exit $?
