#!/usr/bin/env python3
"""BFI IMAX watcher, built to run on GitHub Actions.

Two kinds of page are watched.

1. Discovery pages (the IMAX home page). Alerts on any new film or link,
   and when the Queue-it waiting room goes up.
2. Pages listed in watch_urls.txt (film pages with a list of dates and
   Buy buttons). The script follows the page numbers (1, 2, 3...) and alerts on:
     - a new screening date appearing
     - the number of Buy buttons going up
     - new "on sale" wording (for example "on sale from 12pm")
     - the page no longer saying sold out
     - any other change to the page text

It only reads public pages and messages you on Telegram. You book by hand.

Environment:
    TELEGRAM_TOKEN     from @BotFather
    TELEGRAM_CHAT_ID   from @userinfobot

Usage:
    python imax_watch.py --once
    python imax_watch.py --duration 270 --every 30   # loop for 4.5 minutes
    python imax_watch.py --debug                     # print what it sees
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

VERSION = "3.3"  # bump on every change; shown in Telegram messages

BASE = "https://whatson.bfi.org.uk"
LONDON = ZoneInfo("Europe/London")
# Listing pages: any new link on these is reported as a new listing.
DISCOVERY = [
    f"{BASE}/imax",
    f"{BASE}/imax/Online/default.asp",
    f"{BASE}/imax/Online/default.asp?BOparam::WScontent::loadArticle::permalink="
    "special-screenings-events&BOparam::WScontent::loadArticle::context_id=",
]

HERE = Path(__file__).parent
STATE_FILE = HERE / "imax_state.json"
WATCH_FILE = HERE / "watch_urls.txt"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal ticket alert script)"}
MAX_PAGES = 12  # page numbers followed per film page

LINK_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
STRIP_RE = re.compile(r"<(script|style|noscript|nav|header|footer)\b.*?</\1>", re.I | re.S)
QUEUE_MARKERS = ("queue-it", "queueit", "waiting room", "virtual waiting")
SOLD_OUT_RE = re.compile(r"sold\s*out|no tickets available|no performances", re.I)
DATE_RE = re.compile(
    r"\b(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\s+\d{1,2}\s+[A-Z][a-z]+\s+\d{4}\s+\d{1,2}:\d{2}"
)
DAY = r"(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\s+\d{1,2}\s+[A-Z][a-z]+\s+\d{4}\s+\d{1,2}:\d{2}"
# "On sale Thursday 05 November 2026 12:00" is a sale time, not a screening.
ONSALE_DATE_RE = re.compile(r"\bOn sale\s+(" + DAY + ")", re.I)
BUY_RE = re.compile(r"^(buy|book|buy tickets|book now|book tickets)$", re.I)
ONSALE_RE = re.compile(r"\bon sale\b[^.\n]{0,70}", re.I)
NEXT_TEXT = {">", "›", "»", "next"}
IGNORE = ("createaccount", "signin", "login", "basket", "membership", "bfi-player")
HEARTBEAT_HOURS = 24


def clean(text):
    return " ".join(unescape(TAG_RE.sub(" ", text)).split())


def now():
    return datetime.now(timezone.utc)


def watch_urls():
    """Read watch_urls.txt. Returns (film pages, extra listing pages).

    A line starting with "listing:" is a listing page (new links are alerts).
    Any other line is a film page (dates, Buy buttons and sale times are tracked).
    """
    films, listings = [], []
    if not WATCH_FILE.exists():
        return films, listings
    for line in WATCH_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("listing:"):
            listings.append(line.split(":", 1)[1].strip())
        else:
            films.append(line)
    return films, listings


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
    return r.url, r.text, r.status_code


def parse_page(final_url, html):
    """Return links, dates, buy_count, body text, and pagination urls for one page."""
    links = {}  # url -> (title, is_dated)
    dates = []
    buys = 0
    pages = []
    last_pos = 0
    last_date = None
    host = urlparse(final_url).netloc

    for m in LINK_RE.finditer(html):
        before = ONSALE_DATE_RE.sub(" ", clean(html[last_pos:m.start()]))
        found = DATE_RE.findall(before)
        if found:
            last_date = found[-1]
        last_pos = m.end()

        full = urljoin(final_url, unescape(m.group(1)))
        title = clean(m.group(2))
        low = full.lower()
        if low.startswith("javascript") or "#" == full.strip()[-1:]:
            continue

        # Pagination: same site, link text is a page number or a next arrow.
        if urlparse(full).netloc == host and (
            (title.isdigit() and int(title) >= 2) or title.lower() in NEXT_TEXT
        ):
            pages.append(full)

        if "/imax" not in low or any(b in low for b in IGNORE) or not title:
            continue
        if BUY_RE.match(title):
            buys += 1
            if last_date:
                links[full] = (last_date, True)
            else:
                links[full] = ("Booking link", False)
        else:
            links[full] = (title, False)

    body = clean(STRIP_RE.sub(" ", html))
    for d in DATE_RE.findall(ONSALE_DATE_RE.sub(" ", body)):
        if d not in dates:
            dates.append(d)
    return links, dates, buys, body, pages


def scan(url):
    final_url, html, status = fetch(url)
    low = (final_url + html).lower()
    queue = any(m in low for m in QUEUE_MARKERS)

    all_links, all_dates, buys, bodies = {}, [], 0, []
    todo, seen_pages = [final_url], {final_url}
    count = 0
    first = True
    while todo and count < MAX_PAGES:
        page = todo.pop(0)
        count += 1
        if first:
            first = False
        else:
            try:
                page, html, st = fetch(page)
            except Exception as exc:
                print(f"Could not read page {page}: {exc}", file=sys.stderr)
                continue
            if st >= 400:
                continue
        links, dates, b, body, pages = parse_page(page, html)
        all_links.update(links)
        for d in dates:
            if d not in all_dates:
                all_dates.append(d)
        buys += b
        bodies.append(body)
        for p in pages:
            if p not in seen_pages:
                seen_pages.add(p)
                todo.append(p)

    text = " ".join(bodies)
    return {
        "status": status,
        "queue": queue,
        "links": all_links,
        "dates": all_dates,
        "buys": buys,
        "pages": count,
        "sold_out": bool(SOLD_OUT_RE.search(text)),
        "onsale_dates": sorted(set(ONSALE_DATE_RE.findall(text))),
        "onsale": sorted(
            {clean(s) for s in ONSALE_RE.findall(text) if not ONSALE_DATE_RE.match(s)}
        ),
        "hash": hashlib.sha256(text.encode()).hexdigest()[:16],
        "text": text,
    }


def telegram(msg):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("ALERT (Telegram not set up):", msg)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": msg, "disable_web_page_preview": "true"},
            timeout=20,
        )
    except Exception as exc:
        print(f"Telegram send failed: {exc}", file=sys.stderr)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def check(debug=False):
    state = load_state()
    first_run = state is None
    state = state or {"seen": {}, "pages": {}, "queue": False, "heartbeat": None}
    state.setdefault("disc", [])
    state.setdefault("reminded", [])
    alerts = []
    notes = []
    queue_now = False
    films, extra = watch_urls()
    watched = set(films)
    listings = DISCOVERY + [u for u in extra if u not in DISCOVERY]

    for url in listings + sorted(watched):
        try:
            res = scan(url)
        except Exception as exc:
            print(f"Could not read {url}: {exc}", file=sys.stderr)
            continue

        if debug:
            print(f"\n{url}\n  http={res['status']} queue={res['queue']} sold_out={res['sold_out']} "
                  f"pages_read={res['pages']} dates={len(res['dates'])} buy_buttons={res['buys']}")
            for d in res["dates"]:
                print(f"    date: {d}")
            for s in res["onsale"]:
                print(f"    on sale text: {s}")
            for u, (t, _) in list(res["links"].items())[:40]:
                print(f"    link: {t[:50]:50} {u}")
            print(f"  text: {res['text'][:300]}")

        if res["status"] >= 400:
            print(f"{url} returned {res['status']}, skipping", file=sys.stderr)
            continue

        queue_now = queue_now or res["queue"]
        is_watched = url in watched
        prev = state["pages"].get(url)
        new_page = (is_watched and prev is None) or (
            not is_watched and url not in state["disc"]
        )

        # New links. For watched pages, dated Buy links are reported as dates instead.
        for link, (title, dated) in res["links"].items():
            if link not in state["seen"] and not new_page and not (is_watched and dated):
                alerts.append(f"New on BFI IMAX: {title}\n{link}")
            state["seen"][link] = title

        if not is_watched and url not in state["disc"]:
            state["disc"].append(url)
            notes.append(f"Now watching listing page ({len(res['links'])} links):\n{url}")

        if is_watched:
            if new_page:
                notes.append(f"Now watching {url}\n{len(res['dates'])} dates, "
                             f"{res['buys']} Buy buttons, {res['pages']} page(s) read.")
                if res["onsale_dates"]:
                    notes.append("On sale: " + ", ".join(res["onsale_dates"]))
            else:
                for d in res["onsale_dates"]:
                    if d not in prev.get("onsale_dates", []):
                        alerts.append(f"ON SALE TIME SET: {d}\n{url}")
                for d in res["dates"]:
                    if d not in prev["dates"]:
                        alerts.append(f"NEW SCREENING: {d}\n{url}")
                if res["buys"] > prev["buys"]:
                    alerts.append(f"More Buy buttons: {prev['buys']} to {res['buys']}\n{url}")
                for s in res["onsale"]:
                    if s not in prev["onsale"]:
                        alerts.append(f"On sale info: {s}\n{url}")
                if prev["sold_out"] and not res["sold_out"]:
                    alerts.append(f"No longer sold out. Go now.\n{url}")
                elif prev["hash"] != res["hash"] and not res["queue"] and not any(
                    a.endswith(url) for a in alerts
                ):
                    alerts.append(f"Page changed, worth a look.\n{url}")
            state["pages"][url] = {
                "sold_out": res["sold_out"],
                "hash": res["hash"],
                "dates": res["dates"],
                "buys": res["buys"],
                "onsale": res["onsale"],
                "onsale_dates": res["onsale_dates"],
            }

            # Reminders: 15 minutes before a sale time, and again when it opens.
            for d in res["onsale_dates"]:
                try:
                    when = datetime.strptime(d, "%A %d %B %Y %H:%M").replace(tzinfo=LONDON)
                except ValueError:
                    continue
                mins = (when - now()).total_seconds() / 60
                soon, live = f"{url}|{d}|soon", f"{url}|{d}|live"
                if 0 < mins <= 15 and soon not in state["reminded"]:
                    state["reminded"].append(soon)
                    alerts.append(f"On sale in {int(mins) + 1} minutes ({d}).\n{url}")
                if -5 <= mins <= 0 and live not in state["reminded"]:
                    state["reminded"].append(live)
                    alerts.append(f"ON SALE NOW. Open the page.\n{url}")

    if queue_now and not state["queue"]:
        alerts.insert(0, f"WAITING ROOM IS UP. Tickets are about to drop.\n{DISCOVERY[0]}")
    state["queue"] = queue_now

    last = state.get("heartbeat")
    if first_run:
        alerts = [f"IMAX watcher v{VERSION} is running. Tracking {len(state['seen'])} links "
                  f"and {len(watched)} watched pages."] + notes
        state["heartbeat"] = now().isoformat()
    else:
        alerts = notes + alerts
        if not last or (now() - datetime.fromisoformat(last)).total_seconds() > HEARTBEAT_HOURS * 3600:
            alerts.append(f"Still watching (v{VERSION}). Nothing else to report.")
            state["heartbeat"] = now().isoformat()

    save_state(state)
    for a in alerts:
        telegram(a)
    return alerts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--duration", type=int, default=0, help="keep checking for N seconds")
    ap.add_argument("--every", type=int, default=30, help="seconds between checks")
    args = ap.parse_args()

    if args.debug or args.once or not args.duration:
        check(debug=args.debug)
        return

    end = time.time() + args.duration
    while True:
        check()
        if time.time() + args.every >= end:
            break
        time.sleep(args.every)


if __name__ == "__main__":
    main()
