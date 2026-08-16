#!/usr/bin/env python3
"""Quarterly BWO reference-rate updater — deterministic, no LLM involved.

Fetches the official BWO page, extracts the current hypothekarischer
Referenzzinssatz, and patches the two places the repo stores rate data:

  * index.html         — the <script id="rate-data"> JSON block
  * mietrecht_engine.py — REFERENCE_RATE_SERIES + SERIES_AS_OF/NEXT_ANNOUNCEMENT

Announcement dates are computed, not parsed: the BWO announces on the first
working day of Mar/Jun/Sep/Dec and a changed rate takes effect the following
day (this matches every entry in the series since 2008). Only the rate itself
is read from the page, and it must clear hard sanity gates; on ANY doubt the
script exits 2 without touching a file — the workflow then opens an issue
instead of committing.

Exit codes: 0 = files updated · 78 = verified, nothing to change · 2 = cannot
verify (no files touched).  --check prints findings without writing.
"""

import json
import re
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "index.html"
ENGINE = ROOT / "mietrecht_engine.py"
BWO_URL = "https://www.bwo.admin.ch/de/referenzzinssatz"
QUARTER_MONTHS = (3, 6, 9, 12)


def fail(msg: str) -> None:
    print(f"VERIFICATION FAILED: {msg}", file=sys.stderr)
    sys.exit(2)


def first_working_day(year: int, month: int) -> date:
    d = date(year, month, 1)
    while d.weekday() >= 5:  # Sat/Sun; the 1st of Mar/Jun/Sep/Dec is never a CH holiday
        d += timedelta(days=1)
    return d


def latest_announcement(today: date) -> date:
    candidates = [first_working_day(today.year, m) for m in QUARTER_MONTHS]
    candidates += [first_working_day(today.year - 1, m) for m in QUARTER_MONTHS]
    return max(d for d in candidates if d <= today)


def next_announcement(after: date) -> date:
    candidates = [first_working_day(after.year, m) for m in QUARTER_MONTHS]
    candidates += [first_working_day(after.year + 1, m) for m in QUARTER_MONTHS]
    return min(d for d in candidates if d > after)


def fetch_rate() -> float:
    req = urllib.request.Request(BWO_URL, headers={"User-Agent": "zinscheck.ch updater"})
    try:
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — any network problem means "cannot verify"
        fail(f"could not fetch {BWO_URL}: {e}")
    hits = re.findall(r"Aktueller\s+Referenzzinssatz:\s*([0-9]+[.,][0-9]{1,2})\s*%", html)
    if len(hits) < 2:
        fail(f"rate pattern found {len(hits)} time(s), need >= 2 — page layout may have changed")
    values = {float(h.replace(",", ".")) for h in hits}
    if len(values) != 1:
        fail(f"conflicting rates on page: {sorted(values)}")
    rate = values.pop()
    if abs(rate * 4 - round(rate * 4)) > 1e-9 or not (0.5 <= rate <= 4.0):
        fail(f"implausible rate {rate} (must be a quarter-point value between 0.5 and 4.0)")
    # cross-check: the computed next announcement date must appear on the page
    nxt = next_announcement(date.today())
    if nxt.strftime("%d.%m.%Y") not in html:
        fail(f"computed next announcement {nxt} not found on page — schedule may have moved")
    return rate


def fmt_rate(r: float) -> str:
    return f"{r:.2f}"


def patch_page(series, announced: date, nxt: date) -> None:
    text = PAGE.read_text(encoding="utf-8")
    lines = ",\n".join(f'    ["{d}", {fmt_rate(r)}]' for d, r in series)
    block = (
        '<script id="rate-data" type="application/json">\n'
        "{\n"
        '  "series": [\n' + lines + "\n  ],\n"
        f'  "announcedOn": "{announced}",\n'
        f'  "nextAnnouncement": "{nxt}"\n'
        "}\n"
        "</script>"
    )
    new = re.sub(
        r'<script id="rate-data" type="application/json">.*?</script>',
        block.replace("\\", "\\\\"),
        text,
        count=1,
        flags=re.S,
    )
    if new == text:
        fail("rate-data block not found in index.html")
    PAGE.write_text(new, encoding="utf-8")


def patch_engine(new_entry, announced: date, nxt: date) -> None:
    text = ENGINE.read_text(encoding="utf-8")
    if new_entry is not None:
        eff, rate = new_entry
        insertion = f"    (date({eff.year}, {eff.month}, {eff.day}), {fmt_rate(rate)}),\n]"
        new_text, n = re.subn(
            r"\n\]\n\n# Last BWO announcement",
            "\n" + insertion + "\n\n# Last BWO announcement",
            text,
            count=1,
        )
        if n != 1:
            fail("could not locate end of REFERENCE_RATE_SERIES in mietrecht_engine.py")
        text = new_text
    text, n1 = re.subn(
        r"SERIES_AS_OF: date = date\(\d+, \d+, \d+\)",
        f"SERIES_AS_OF: date = date({announced.year}, {announced.month}, {announced.day})",
        text, count=1,
    )
    text, n2 = re.subn(
        r"NEXT_ANNOUNCEMENT: date = date\(\d+, \d+, \d+\)",
        f"NEXT_ANNOUNCEMENT: date = date({nxt.year}, {nxt.month}, {nxt.day})",
        text, count=1,
    )
    if n1 != 1 or n2 != 1:
        fail("SERIES_AS_OF / NEXT_ANNOUNCEMENT constants not found in mietrecht_engine.py")
    ENGINE.write_text(text, encoding="utf-8")


def main() -> None:
    check_only = "--check" in sys.argv
    today = date.today()
    announced = latest_announcement(today)
    nxt = next_announcement(announced)

    data = json.loads(
        re.search(
            r'<script id="rate-data" type="application/json">\s*(\{.*?\})\s*</script>',
            PAGE.read_text(encoding="utf-8"), re.S,
        ).group(1)
    )
    series = [(d, float(r)) for d, r in data["series"]]
    last_rate = series[-1][1]

    rate = fetch_rate()
    print(f"BWO page rate: {fmt_rate(rate)} % | stored last: {fmt_rate(last_rate)} %")
    print(f"announcement: {announced} | next: {nxt} | stored announcedOn: {data['announcedOn']}")

    if abs(rate - last_rate) > 0.25 + 1e-9:
        fail(f"rate moved more than one quarter-point ({last_rate} -> {rate}) — needs human review")

    new_entry = None
    if abs(rate - last_rate) > 1e-9:
        eff = announced + timedelta(days=1)
        series.append((str(eff), rate))
        new_entry = (eff, rate)
        print(f"rate CHANGED -> appending [{eff}, {fmt_rate(rate)}]")

    if new_entry is None and data["announcedOn"] == str(announced) and data["nextAnnouncement"] == str(nxt):
        print("nothing to change — data already current")
        sys.exit(78)

    if check_only:
        print("--check: changes pending, no files written")
        sys.exit(0)

    patch_page(series, announced, nxt)
    patch_engine(new_entry, announced, nxt)
    print("updated index.html and mietrecht_engine.py")
    sys.exit(0)


if __name__ == "__main__":
    main()
