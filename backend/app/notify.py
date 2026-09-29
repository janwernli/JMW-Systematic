"""Push notifications via ntfy (https://ntfy.sh). Off unless NTFY_TOPIC is set.

Anyone who knows a public ntfy.sh topic name can read its messages, so use a long random topic
(or your own ntfy server / an access token). Messages contain equity and order counts, never keys.
Sending never raises: a failed notification is logged and must not break a trading run.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


def send(settings, message: str, title: str = "JMW Trading", priority: str = "default",
         tags: tuple[str, ...] = (), transport: httpx.BaseTransport | None = None) -> bool:
    topic = (settings.ntfy_topic or "").strip()
    if not topic:
        return False
    headers = {"Title": title.encode("ascii", "replace").decode(), "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    if settings.ntfy_token is not None and settings.ntfy_token.get_secret_value():
        headers["Authorization"] = f"Bearer {settings.ntfy_token.get_secret_value()}"
    url = f"{settings.ntfy_server.rstrip('/')}/{topic}"
    try:
        with httpx.Client(timeout=10, transport=transport) as c:
            r = c.post(url, content=message.encode("utf-8"), headers=headers)
            r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001 - notifications are best effort
        log.warning("ntfy notification failed", extra={"error": str(e)})
        return False


def _step(res: dict, name: str) -> dict:
    return next((s for s in res.get("steps", []) if s.get("name") == name), {})


def daily_summary(res: dict, trigger: str = "", dry_run: bool = False) -> tuple[str, str, str, tuple[str, ...]]:
    """(title, message, priority, tags) for one daily-cycle result."""
    data, sync, orders = _step(res, "data"), _step(res, "sync"), _step(res, "orders")
    d, s, o = data.get("data") or {}, sync.get("data") or {}, orders.get("data") or {}
    stale = d.get("fresh") is False
    errors = [f"{st['name']}: {st.get('detail', '')}" for st in res.get("steps", []) if st.get("status") == "error"]
    lines = []
    if o and "sent" in o:
        line = f"Orders: {o['sent']} sent, {o['planned']} planned, {o['skipped']} skipped"
        if o.get("awaiting"):
            line += f", {o['awaiting']} awaiting approval"
        lines.append(line)
    elif orders:
        lines.append(f"Orders: {orders.get('detail', 'none')}")
    if "equity" in s:
        lines.append(f"Equity ${s['equity']:,.2f} | {s.get('longs', 0)} long / {s.get('shorts', 0)} short")
    if d:
        lines.append(f"Data through {d.get('latest')}" + (f" - STALE (expected {d.get('expected')})" if stale else ""))
    lines += [f"ERROR {e}" for e in errors]
    if o.get("awaiting"):
        lines.append("Approve or decline the plan on the Trading page.")
    tag = "DRY RUN " if dry_run else ""
    if errors or res.get("status") == "error":
        title, prio, tags = f"JMW daily {tag}ERROR", "high", ("rotating_light",)
        if stale and not [e for e in errors if not e.startswith("data:")]:
            title = f"JMW daily {tag}STALE DATA"
    elif o.get("awaiting"):
        title, prio, tags = f"JMW daily {tag}- plan awaiting approval", "high", ("inbox_tray",)
    elif res.get("status") == "warning":
        title, prio, tags = f"JMW daily {tag}warning", "default", ("warning",)
    else:
        title, prio, tags = f"JMW daily {tag}ok", "low" if not o.get("sent") else "default", ("white_check_mark",)
    msg = f"Run #{res.get('run_id')} ({trigger or 'manual'})\n" + "\n".join(lines)
    return title, msg, prio, tags


def notify_daily(settings, res: dict, trigger: str = "", dry_run: bool = False,
                 transport: httpx.BaseTransport | None = None) -> bool:
    title, msg, prio, tags = daily_summary(res, trigger, dry_run)
    return send(settings, msg, title=title, priority=prio, tags=tags, transport=transport)


def alert(settings, what: str, detail: str, transport: httpx.BaseTransport | None = None) -> bool:
    return send(settings, detail[:3000], title=f"JMW {what} FAILED", priority="high", tags=("rotating_light",),
                transport=transport)
