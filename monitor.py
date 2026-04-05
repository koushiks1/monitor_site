#!/usr/bin/env python3
"""
Poll a URL, compare a structural snapshot (interactive elements + key attributes),
and email when anything changes.

Default URL targets RCB ticket shop:
  https://shop.royalchallengers.com/ticket

For pages where buttons/state are set only after JavaScript runs, set
MONITOR_USE_PLAYWRIGHT=1 and install Playwright (see requirements.txt).
"""

from __future__ import annotations

import requests
import argparse
import hashlib
import json
import os
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import unified_diff
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

DEFAULT_URL = "https://shop.royalchallengers.com/ticket"
STATE_NAME = ".monitor_state.json"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default

def send_slack(message: str):
    webhook = os.environ.get("SLACK_WEBHOOK_URL")

    if not webhook:
        print("Slack not configured")
        return

    payload = {
        "text": f"<!channel> 🚨 {message[:500]}"
    }

    response = requests.post(webhook, json=payload)
    print("Slack status:", response.status_code)

def _text(el) -> str:
    from bs4 import BeautifulSoup

    if el is None:
        return ""
    t = el.get_text(separator=" ", strip=True)
    return " ".join(t.split())[:400]


def _attrs_dict(el) -> dict[str, str]:
    raw = getattr(el, "attrs", None) or {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if k in ("style",):
            continue
        if isinstance(v, list):
            out[k] = " ".join(str(x) for x in v)
        else:
            out[k] = str(v)
    return out


def element_fingerprint(el, index: int) -> dict[str, Any]:
    """Stable, comparable description of an element (for diffing)."""
    name = el.name.lower() if getattr(el, "name", None) else "?"
    parent = getattr(el, "parent", None)
    parent_name = parent.name.lower() if parent and getattr(parent, "name", None) else ""
    attrs = _attrs_dict(el)
    # Short path hint: parent > tag + index among siblings of same tag
    path_hint = f"{parent_name}>{name}[{index}]"
    return {
        "path_hint": path_hint,
        "tag": name,
        "attrs": dict(sorted(attrs.items())),
        "text": _text(el),
    }


def _sibling_tag_index(el) -> int:
    parent = el.parent
    if not parent or not getattr(el, "name", None):
        return 0
    same = [c for c in parent.children if getattr(c, "name", None) == el.name]
    try:
        return same.index(el)
    except ValueError:
        return 0


def collect_snapshots(soup) -> list[dict[str, Any]]:
    """Collect interactive and high-signal elements (buttons, links, inputs, etc.)."""
    selectors = ["button","a[href]","input","select","textarea","role='button']","div","span"]
    seen_set: set[int] = set()
    rows: list[dict[str, Any]] = []

    for sel in selectors:
        for el in soup.select(sel):
            eid = id(el)
            if eid in seen_set:
                continue
            seen_set.add(eid)
            idx = _sibling_tag_index(el)
            rows.append(element_fingerprint(el, idx))

    rows.sort(key=lambda r: (r["tag"], r["path_hint"], json.dumps(r["attrs"], sort_keys=True), r["text"]))
    return rows


def collect_raw_digest(soup) -> str:
    """Fallback: normalized text + tag counts when selector scope is empty."""
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    lines = [ln for ln in (x.strip() for x in text.splitlines()) if ln]
    return "\n".join(lines[:2000])


def fetch_html_http(url: str, timeout: float) -> str:
    import requests

    r = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.text


def fetch_html_playwright(url: str, root_selector: str | None, wait_ms: int, timeout_ms: int) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        ) from e

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)

            # Load page
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            # ⏱️ Wait for JS to load content
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)

            # 🔥 Scroll to trigger lazy loading
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(3000)

            # 📸 Take screenshot AFTER everything loads
            page.screenshot(path="debug.png", full_page=True)

            # 🎯 Try to capture only target section
            if root_selector:
                try:
                    page.wait_for_selector(root_selector, timeout=min(10_000, timeout_ms))
                except Exception:
                    pass

                handles = page.query_selector_all(root_selector)
                if handles:
                    handle = handles[-1]   # 🔥 pick LAST occurrence (usually tickets)
                    return handle.evaluate("el => el.outerHTML")

            # fallback
            return page.content()

        finally:
            browser.close()


def build_snapshot(html: str, root_selector: str | None) -> tuple[list[dict[str, Any]], str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    if root_selector:
        root = soup.select_one(root_selector)
        if root:
            soup = BeautifulSoup(str(root), "html.parser")
    rows = collect_snapshots(soup)
    digest = collect_raw_digest(soup)
    if not rows and digest:
        # Ensure we still detect content-only changes
        rows = [
            {
                "path_hint": "body:text-digest",
                "tag": "_text_digest",
                "attrs": {},
                "text": digest[:8000],
            }
        ]
    return rows, digest


def fingerprint(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def diff_snapshots(old_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> str:
    a = json.dumps(old_rows, indent=2, sort_keys=True, ensure_ascii=False).splitlines(keepends=True)
    b = json.dumps(new_rows, indent=2, sort_keys=True, ensure_ascii=False).splitlines(keepends=True)
    return "".join(
        unified_diff(a, b, fromfile="previous", tofile="current", lineterm="")
    )


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    mail_from: str
    mail_to: str

    @classmethod
    def from_env(cls) -> SmtpConfig | None:
        host = os.environ.get("SMTP_HOST", "").strip()
        port_s = os.environ.get("SMTP_PORT", "587").strip()
        user = os.environ.get("SMTP_USER", "").strip()
        password = os.environ.get("SMTP_PASSWORD", "").strip()
        mail_from = os.environ.get("EMAIL_FROM", user).strip()
        mail_to = os.environ.get("EMAIL_TO", "").strip()
        if not all([host, user, password, mail_to]):
            return None
        try:
            port = int(port_s)
        except ValueError:
            port = 587
        return cls(host=host, port=port, user=user, password=password, mail_from=mail_from, mail_to=mail_to)


def send_email(cfg: SmtpConfig, subject: str, body_text: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg.mail_from

    # ✅ Split multiple emails
    to_list = [email.strip() for email in cfg.mail_to.split(",") if email.strip()]
    msg["To"] = ", ".join(to_list)

    # 🔥 Mark as HIGH PRIORITY
    msg["X-Priority"] = "1"          # 1 = High, 3 = Normal, 5 = Low
    msg["X-MSMail-Priority"] = "High"
    msg["Importance"] = "High"

    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    with smtplib.SMTP(cfg.host, cfg.port, timeout=60) as server:
        server.starttls()
        server.login(cfg.user, cfg.password)
        server.sendmail(cfg.mail_from, to_list, msg.as_string())

    print(f"High priority email sent to: {', '.join(to_list)}")


def _state_redis_key() -> str:
    return os.environ.get("STATE_REDIS_KEY", "site-monitor:state")


def _upstash_enabled() -> bool:
    return bool(
        os.environ.get("UPSTASH_REDIS_REST_URL", "").strip()
        and os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
    )


def uses_upstash_state() -> bool:
    """True when snapshot state is stored in Upstash (required for Vercel)."""
    return _upstash_enabled()


def _upstash_get_json(base_url: str, token: str, key: str) -> dict[str, Any]:
    import requests
    import json

    k = quote(key, safe="")
    r = requests.get(
        f"{base_url.rstrip('/')}/get/{k}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    out = r.json()

    res = out.get("result")

    if res is None:
        return {}

    # 🔥 FIX: convert string → dict
    if isinstance(res, str):
        try:
            return json.loads(res)
        except json.JSONDecodeError:
            return {}

    return {}


def _upstash_set_json(base_url: str, token: str, key: str, data: dict[str, Any]) -> None:
    import requests
    import json

    k = quote(key, safe="")
    raw = json.dumps(data, ensure_ascii=False)

    r = requests.post(
        f"{base_url.rstrip('/')}/set/{k}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=raw.encode("utf-8"),   # ✅ no double encoding
        timeout=30,
    )
    r.raise_for_status()


def load_state(path: Path) -> dict[str, Any]:
    if _upstash_enabled():
        return _upstash_get_json(
            os.environ["UPSTASH_REDIS_REST_URL"].strip(),
            os.environ["UPSTASH_REDIS_REST_TOKEN"].strip(),
            _state_redis_key(),
        )
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, data: dict[str, Any]) -> None:
    if _upstash_enabled():
        _upstash_set_json(
            os.environ["UPSTASH_REDIS_REST_URL"].strip(),
            os.environ["UPSTASH_REDIS_REST_TOKEN"].strip(),
            _state_redis_key(),
            data,
        )
        return
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


@dataclass
class MonitorResult:
    exit_code: int
    event: str
    detail: str = ""


def run_once(
    url: str,
    state_path: Path,
    smtp: SmtpConfig | None,
    use_playwright: bool,
    root_selector: str | None,
    wait_ms: int,
    timeout: float,
    dry_run: bool,
    force_baseline: bool,
) -> MonitorResult:
    timeout_ms = int(timeout * 1000)
    if use_playwright:
        html = fetch_html_playwright(url, root_selector, wait_ms, timeout_ms)
    else:
        html = fetch_html_http(url, timeout)

    rows, _digest = build_snapshot(html, root_selector)
    rows = [
    r for r in rows
    if r.get("text") and len(r.get("text").strip()) > 3
    ]
    print("----- DEBUG: SNAPSHOT ELEMENTS -----")
    for r in rows[:20]:   # limit to first 20 to avoid huge logs
        print("TEXT:", r.get("text"))
    print("----- END DEBUG -----")
    important_rows = [
    r for r in rows
    if any(k in r.get("text", "").lower()
           for k in ["rcb", "csk", "vs", "ticket", "buy"])
    ]

    send_slack("MATCH DEBUG:\n" + "\n".join([r.get("text", "") for r in important_rows[:20]]) if important_rows else "❌ No match data")
    fp = fingerprint(rows)
    state = load_state(state_path)
    prev_fp = state.get("fingerprint")
    prev_rows = state.get("rows")

    host = urlparse(url).netloc or url
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if prev_fp is None or force_baseline:
        save_state(state_path, {"fingerprint": fp, "rows": rows, "url": url, "updated_at": ts})
        msg = f"Baseline saved ({len(rows)} elements). fingerprint={fp[:16]}…"
        print(msg)
        return MonitorResult(0, "baseline", msg)

    if fp == prev_fp:
        msg = f"No change. fingerprint={fp[:16]}…"
        print(msg)
        return MonitorResult(0, "no_change", msg)

    diff_text = ""
    if isinstance(prev_rows, list):
        diff_text = diff_snapshots(prev_rows, rows)
    if len(diff_text) > 120_000:
        diff_text = diff_text[:120_000] + "\n… (truncated)"

    body = (
        f"Page change detected\n\n"
        f"URL: {url}\n"
        f"Time: {ts}\n"
        f"Previous fingerprint: {prev_fp}\n"
        f"Current fingerprint:  {fp}\n\n"
        f"--- JSON diff (interactive elements) ---\n"
        f"{diff_text or '(no structured diff; row shape may have changed)'}\n"
    )

    subject = f"[Site monitor] Change on {host}"

    if dry_run:
        print(subject)
        print(body[:4000])
        if len(body) > 4000:
            print("…")
        return MonitorResult(0, "dry_run_change", subject)
    if smtp:
        send_email(smtp, subject, body)
        send_slack(f"{subject}\n\n{body[:1000]}")
        print(f"Email sent to {smtp.mail_to}")
    else:
        print("Change detected but SMTP is not configured. Set SMTP_* and EMAIL_TO.", file=sys.stderr)
        print(body[:8000], file=sys.stderr)
        return MonitorResult(2, "no_smtp", "SMTP not configured on change")

    save_state(state_path, {"fingerprint": fp, "rows": rows, "url": url, "updated_at": ts})
    return MonitorResult(0, "emailed", smtp.mail_to)


def main() -> int:
    p = argparse.ArgumentParser(description="Monitor a URL and email on DOM snapshot changes.")
    _url = (os.environ.get("MONITOR_URL") or "").strip() or DEFAULT_URL
    p.add_argument("--url", default=_url)
    p.add_argument("--state", type=Path, default=Path(__file__).resolve().parent / STATE_NAME)
    p.add_argument(
        "--interval",
        type=int,
        default=_env_int("MONITOR_INTERVAL", 0),
        help="Seconds between checks; 0 = run once. Env: MONITOR_INTERVAL (e.g. 600 = 10 min).",
    )
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument(
        "--use-playwright",
        action="store_true",
        default=os.environ.get("MONITOR_USE_PLAYWRIGHT", "").strip() in ("1", "true", "yes"),
        help="Render with Chromium (captures JS-driven button state).",
    )
    p.add_argument(
        "--root-selector",
        default=os.environ.get("MONITOR_ROOT_SELECTOR", "").strip() or None,
        help="Optional CSS selector to scope the snapshot.",
    )
    p.add_argument(
        "--wait-ms",
        type=int,
        default=int(os.environ.get("MONITOR_WAIT_MS", "3000")),
        help="Extra wait after load (Playwright only).",
    )
    p.add_argument("--dry-run", action="store_true", help="Print diff to stdout; do not send email.")
    p.add_argument("--force-baseline", action="store_true", help="Overwrite state without comparing.")
    args = p.parse_args()

    smtp = None if args.dry_run else SmtpConfig.from_env()
    if not args.dry_run and smtp is None:
        print("Warning: SMTP not fully configured; changes will print to stderr only.", file=sys.stderr)

    if args.interval and args.interval > 0:
        print(
            f"Polling every {args.interval} s (~{args.interval / 60:.1f} min); Ctrl+C to stop.",
            flush=True,
        )
        while True:
            try:
                run_once(
                    url=args.url,
                    state_path=args.state,
                    smtp=smtp,
                    use_playwright=args.use_playwright,
                    root_selector=args.root_selector,
                    wait_ms=args.wait_ms,
                    timeout=args.timeout,
                    dry_run=args.dry_run,
                    force_baseline=args.force_baseline,
                )
            except KeyboardInterrupt:
                return 130
            except Exception as ex:
                print(f"Check failed: {ex}", file=sys.stderr)
            time.sleep(args.interval)
    result = run_once(
        url=args.url,
        state_path=args.state,
        smtp=smtp,
        use_playwright=args.use_playwright,
        root_selector=args.root_selector,
        wait_ms=args.wait_ms,
        timeout=args.timeout,
        dry_run=args.dry_run,
        force_baseline=args.force_baseline,
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
