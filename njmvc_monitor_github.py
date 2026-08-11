#!/usr/bin/env python3
"""
NJ MVC Saturday Non-Driver ID monitor for GitHub Actions.

Targets:
- Bayonne (210)
- North Bergen (224)
- Newark (223)

Behavior:
- Checks only Saturday dates.
- Verifies actual time-slot links before alerting.
- Sends email only for newly appearing verified slots.
- Persists currently-active alerted slot state in .njmvc_state.json.
- Uses GMAIL_ADDRESS and GMAIL_APP_PASSWORD environment variables.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import smtplib
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BASE = "https://telegov.njportal.com"
APPOINTMENT_ID = 16
DURATION = 15
TARGET_WEEKDAY = 5  # Saturday

LOCATIONS = {
    "Bayonne": 210,
    "North Bergen": 224,
    "Newark": 223,
}

NY = ZoneInfo("America/New_York")
STATE_FILE = Path(__file__).resolve().parent / "njmvc_state.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


def log(message: str) -> None:
    stamp = datetime.now(NY).strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{stamp}] {message}")


def http_get(url: str, timeout: int = 25) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        method="GET",
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def month_starts_between(start: date, end: date):
    cur = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while cur <= last:
        yield cur
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)


def availability_url(location_id: int, month_start: date) -> str:
    params = {
        "duration": DURATION,
        "locationId": location_id,
        "appointmentId": APPOINTMENT_ID,
        "date": f"{month_start.isoformat()}T00:00:00.000Z",
    }
    return (
        f"{BASE}/njmvc/CustomerCreateAppointments/"
        f"GetAvailableDatesForMonth?{urlencode(params)}"
    )


def fetch_available_dates(location_id: int, month_start: date) -> list[date]:
    raw = http_get(availability_url(location_id, month_start))
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("Availability endpoint did not return a JSON list.")
    return [date.fromisoformat(str(item)[:10]) for item in data]


class SlotParser(HTMLParser):
    def __init__(self, location_id: int, day: date):
        super().__init__()
        self.prefix = (
            f"/njmvc/AppointmentWizard/{APPOINTMENT_ID}/"
            f"{location_id}/{day.isoformat()}/"
        )
        self.current_href = None
        self.current_text = []
        self.slots = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href", "")
            if href.startswith(self.prefix):
                self.current_href = href
                self.current_text = []

    def handle_data(self, data):
        if self.current_href is not None:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.current_href is not None:
            text = " ".join(" ".join(self.current_text).split())
            match = re.search(
                r"\b(\d{1,2}:\d{2}\s+(?:AM|PM)(?:\s+[A-Z]{2,5})?)\b",
                text,
            )
            if match:
                self.slots.append(
                    {
                        "time": match.group(1),
                        "token": self.current_href.rsplit("/", 1)[-1],
                        "url": BASE + self.current_href,
                    }
                )
            self.current_href = None
            self.current_text = []


def fetch_slots(location_id: int, day: date) -> list[dict]:
    page_url = (
        f"{BASE}/njmvc/AppointmentWizard/{APPOINTMENT_ID}/"
        f"{location_id}?date={day.isoformat()}"
    )
    parser = SlotParser(location_id, day)
    parser.feed(http_get(page_url))

    seen = set()
    result = []
    for slot in parser.slots:
        if slot["url"] not in seen:
            seen.add(slot["url"])
            result.append(slot)
    return result


def slot_key(location: str, day: date, slot: dict) -> str:
    return f"{location}|{day.isoformat()}|{slot['token']}"


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"active_by_location": {}}


def save_state(active_by_location: dict) -> None:
    STATE_FILE.write_text(
        json.dumps({"active_by_location": active_by_location}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def gmail_credentials() -> tuple[str, str]:
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not address:
        raise RuntimeError("GMAIL_ADDRESS is missing.")
    if not password:
        raise RuntimeError("GMAIL_APP_PASSWORD is missing.")
    return address, password


def send_email(subject: str, body: str) -> None:
    address, password = gmail_credentials()

    msg = EmailMessage()
    msg["From"] = address
    msg["To"] = address
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(address, password)
        smtp.send_message(msg)


def send_test_email() -> int:
    send_email(
        "NJ MVC GitHub monitor test",
        (
            "Success — GitHub Actions can send email through your Gmail account.\n\n"
            "The real monitor is configured for:\n"
            "- Non-Driver ID\n"
            "- Bayonne\n"
            "- North Bergen\n"
            "- Newark\n"
            "- Saturday only\n"
        ),
    )
    log("Test email sent successfully.")
    return 0


def collect_verified_slots() -> dict[str, list[dict]]:
    today = datetime.now(NY).date()
    # The MVC page observed in testing exposed roughly a one-month booking horizon.
    # Query enough months to cover the next 35 days.
    horizon = today + timedelta(days=35)

    active_by_location: dict[str, list[dict]] = {}

    for location, location_id in LOCATIONS.items():
        dates = set()

        for month_start in month_starts_between(today, horizon):
            for day in fetch_available_dates(location_id, month_start):
                if today <= day <= horizon and day.weekday() == TARGET_WEEKDAY:
                    dates.add(day)

        verified = []
        for day in sorted(dates):
            # Do not trust the calendar date alone. Verify real selectable time links.
            for slot in fetch_slots(location_id, day):
                verified.append(
                    {
                        "key": slot_key(location, day, slot),
                        "location": location,
                        "location_id": location_id,
                        "date": day.isoformat(),
                        "time": slot["time"],
                        "url": slot["url"],
                    }
                )

        active_by_location[location] = verified
        log(f"{location}: {len(verified)} verified Saturday time slot(s).")

    return active_by_location


def format_alert(new_items: list[dict]) -> str:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for item in new_items:
        grouped.setdefault((item["location"], item["date"]), []).append(item)

    lines = [
        "A new NJ MVC Saturday Non-Driver ID appointment is available.",
        "",
    ]

    for (location, day_iso), items in sorted(grouped.items()):
        day = date.fromisoformat(day_iso)
        lines.append(f"{location} — {day.strftime('%A, %B %d, %Y')}")
        lines.append("Times: " + ", ".join(x["time"] for x in items))
        lines.append("Book: " + items[0]["url"])
        lines.append("")

    lines.extend(
        [
            "Monitored locations: Bayonne, North Bergen, Newark.",
            "This monitor only alerts after verifying actual appointment time links.",
        ]
    )
    return "\n".join(lines)


def check() -> int:
    previous_state = load_state()
    previous_active = previous_state.get("active_by_location", {})

    current = collect_verified_slots()

    new_items = []
    serializable_current = {}

    for location in LOCATIONS:
        items = current.get(location, [])
        serializable_current[location] = sorted(item["key"] for item in items)
        old_keys = set(previous_active.get(location, []))
        for item in items:
            if item["key"] not in old_keys:
                new_items.append(item)

    if new_items:
        body = format_alert(new_items)
        # If email fails, do NOT save the new state. That way the next run retries.
        send_email("🚨 NJ MVC SATURDAY SLOT FOUND", body)
        log(f"Alert email sent for {len(new_items)} newly appearing slot(s).")
    else:
        log("No new Saturday slots. No email sent.")

    # Save currently active slots. If a slot disappears and later reappears,
    # it becomes "new" again and will alert again.
    save_state(serializable_current)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Send a test email and do not check NJ MVC.",
    )
    args = parser.parse_args()

    try:
        if args.test_email:
            return send_test_email()
        return check()
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        log(f"Check failed: {type(exc).__name__}: {exc}")
        return 2
    except Exception as exc:
        log(f"Monitor failed: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
