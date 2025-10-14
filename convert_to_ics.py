#!/usr/bin/env python3
"""
convert_to_ics.py

Parse a simple plaintext event list and produce an iCalendar (.ics) file.

Usage:
    python convert_to_ics.py events.txt
    python convert_to_ics.py events.txt --min-date 2025-10-14 --out calendar.ics
    cat events.txt | python convert_to_ics.py - -m 2025-10-14

This version fixes a crash when an end time is "24:00" (or otherwise outside 0..23).
Behavior / assumptions (same as before):
- Timezone: Europe/Zurich (CET/CEST). ICS will include a VTIMEZONE block.
- Lines must begin with an ISO date: YYYY-MM-DD
- Time formats:
    * "HH:MM"                -> event with 1h default duration
    * "HH:MM-HH:MM"         -> event with explicit start/end (end may be 24:00)
    * absent time           -> all-day event (VALUE=DATE)
- HTML anchors like <a href="...">text</a> are parsed:
    * anchor text used in summary (replaces the HTML)
    * href used as event URL (file: if local path begins with "/")
- Comments after '#' become DESCRIPTION text (appended)
- Multi-day events:
    * "begin" marks start; the matching "end" marks the end (inclusive)
- Lines with a trailing numeric token (e.g., "grundsteuerabbuchung 1800")
  will include "Amount: XXXX" in DESCRIPTION.
- Only events whose start date/time is >= --min-date are included (start date inclusive).
"""

import sys
import re
import html
import argparse
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional, List, Dict, Tuple

TZ = ZoneInfo("Europe/Zurich")
OWNER = "jeremytammik"  # used for UID generation; change as needed

# Static VTIMEZONE block for Europe/Zurich (simple representation)
VTIMEZONE_BLOCK = """BEGIN:VTIMEZONE
TZID:Europe/Zurich
X-LIC-LOCATION:Europe/Zurich
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE
"""

# Regex helpers
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(.*)$")
TIME_RANGE_RE = re.compile(r"^(\d{1,2}:\d{2})(?:-(\d{1,2}:\d{2}))?\s+(.*)$")
ANCHOR_RE = re.compile(r'<a\s+href="([^"]+)">([^<]+)</a>', flags=re.IGNORECASE)

def parse_anchor(text: str) -> Tuple[str, Optional[str]]:
    """Replace first anchor with its text and return (new_text, url or None)."""
    m = ANCHOR_RE.search(text)
    if not m:
        return text, None
    href, label = m.group(1), m.group(2)
    new_text = ANCHOR_RE.sub(label, text, count=1)
    if href.startswith("/"):
        url = "file://" + href
    else:
        url = href
    return new_text, url

def slugify(s: str) -> str:
    s2 = re.sub(r"\s+", "_", (s or "").strip().lower())
    s2 = re.sub(r"[^a-z0-9_@-]", "", s2)
    return s2[:80] or "event"

class ParsedLine:
    def __init__(self, dt: date,
                 start: Optional[time], start_offset: int,
                 end: Optional[time], end_offset: int,
                 summary: str, url: Optional[str], description: Optional[str],
                 raw: str, is_begin: bool=False, is_end: bool=False):
        self.dt = dt
        self.start = start
        self.start_offset = start_offset  # days to add to dt for start (0 or 1)
        self.end = end
        self.end_offset = end_offset      # days to add to dt for end (0 or 1)
        self.summary = summary
        self.url = url
        self.description = description
        self.raw = raw
        self.is_begin = is_begin
        self.is_end = is_end
        self.consumed = False  # for multi-day grouping

def parse_time(tstr: str) -> Tuple[time, int]:
    """
    Parse a HH:MM string.
    Returns (time_obj, day_offset).
    day_offset is 1 when time is "24:00" -> interpreted as 00:00 next day.
    Raises ValueError for invalid times.
    """
    parts = tstr.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: {tstr!r}")
    try:
        h = int(parts[0])
        m = int(parts[1])
    except ValueError:
        raise ValueError(f"Invalid time numbers: {tstr!r}")
    if h == 24 and m == 0:
        return time(0, 0), 1
    if 0 <= h <= 23 and 0 <= m <= 59:
        return time(h, m), 0
    raise ValueError(f"Hour must be in 0..23 and minutes in 0..59, got: {tstr!r}")

def parse_lines(lines: List[str]) -> List[ParsedLine]:
    parsed: List[ParsedLine] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        # ignore code fences if present
        if line.startswith("```") or line.endswith("```"):
            continue
        m = DATE_RE.match(line)
        if not m:
            continue
        date_s, rest = m.group(1), m.group(2).strip()
        try:
            dt = datetime.strptime(date_s, "%Y-%m-%d").date()
        except ValueError:
            continue
        # split comment after '#'
        if "#" in rest:
            main, comment = rest.split("#", 1)
            comment = comment.strip()
        else:
            main, comment = rest, None
        main = main.strip()
        # parse anchor tags
        main_processed, anchor_url = parse_anchor(main)
        # detect time range at beginning of main_processed
        start_t, start_off = None, 0
        end_t, end_off = None, 0
        summary_part = main_processed
        mtime = TIME_RANGE_RE.match(main_processed)
        if mtime:
            start_s = mtime.group(1)
            end_s = mtime.group(2)
            summary_part = mtime.group(3).strip()
            try:
                start_t, start_off = parse_time(start_s)
            except ValueError as e:
                print(f"Warning: invalid start time on line: {raw!r} -> {e}", file=sys.stderr)
                # skip this line
                continue
            if end_s:
                try:
                    end_t, end_off = parse_time(end_s)
                except ValueError as e:
                    print(f"Warning: invalid end time on line: {raw!r} -> {e}", file=sys.stderr)
                    continue
        # detect trailing numeric amount token (e.g., "grundsteuerabbuchung 1800")
        amount_desc = None
        amt_m = re.match(r"^(.*\S)\s+(\d{2,})(?:\s*)$", summary_part)
        if amt_m and comment is None:
            summary_part = amt_m.group(1)
            amount_desc = f"Amount: {amt_m.group(2)}"
        # detect begin/end tokens in summary_part
        is_begin = False
        is_end = False
        sp_lower = summary_part.lower()
        if sp_lower.endswith(" begin"):
            is_begin = True
            summary_part = summary_part[:-6].strip()
        elif sp_lower.endswith(" end"):
            is_end = True
            summary_part = summary_part[:-4].strip()
        # final cleanup and html-unescape
        summary = html.unescape(summary_part).strip()
        desc_parts = []
        if comment:
            desc_parts.append(html.unescape(comment))
        if amount_desc:
            desc_parts.append(amount_desc)
        description = "; ".join(desc_parts) if desc_parts else None
        # choose url preferentially from anchor tag, otherwise none
        url = anchor_url
        parsed.append(ParsedLine(dt=dt,
                                 start=start_t, start_offset=start_off,
                                 end=end_t, end_offset=end_off,
                                 summary=summary, url=url,
                                 description=description, raw=raw,
                                 is_begin=is_begin, is_end=is_end))
    return parsed

def group_multiday(parsed: List[ParsedLine]) -> List[Dict]:
    """
    Detect multi-day events from sequences with same summary and begin/end markers.
    Returns a list of event dicts with keys:
      - type: "timed" or "allday" or "multiday"
      - dtstart, dtend (date or datetime)
      - summary, url, description
    """
    events: List[Dict] = []
    parsed = sorted(parsed, key=lambda p: p.dt)
    ongoing: Dict[str, List[Tuple[int, date, ParsedLine]]] = {}
    for i, p in enumerate(parsed):
        if p.is_begin:
            key = slugify(p.summary)
            ongoing.setdefault(key, []).append((i, p.dt, p))
            p.consumed = True
        elif p.is_end:
            key = slugify(p.summary)
            if key in ongoing and ongoing[key]:
                start_index, start_date, start_parsed = ongoing[key].pop(0)
                # mark consumed for intermediate lines that share the same summary
                for j in range(start_index, i+1):
                    if slugify(parsed[j].summary) == key:
                        parsed[j].consumed = True
                sd = start_date
                ed = p.dt
                events.append({
                    "type": "multiday",
                    "dtstart": sd,
                    "dtend": ed + timedelta(days=1),  # DTEND exclusive
                    "summary": start_parsed.summary or p.summary,
                    "url": start_parsed.url or p.url,
                    "description": start_parsed.description or p.description
                })
            else:
                # unmatched end -> treat as single all-day
                p.consumed = True
                events.append({
                    "type": "allday",
                    "dtstart": p.dt,
                    "dtend": p.dt + timedelta(days=1),
                    "summary": p.summary,
                    "url": p.url,
                    "description": p.description
                })
    # remaining unconsumed lines -> single events
    for p in parsed:
        if p.consumed:
            continue
        if p.start or p.end:
            # compute start datetime taking into account offsets
            start_dt = datetime.combine(p.dt + timedelta(days=p.start_offset),
                                        p.start or time(hour=0, minute=0)).replace(tzinfo=TZ)
            if p.end:
                end_dt = datetime.combine(p.dt + timedelta(days=p.end_offset),
                                          p.end).replace(tzinfo=TZ)
            else:
                # default 1 hour duration
                end_dt = (datetime.combine(p.dt + timedelta(days=p.start_offset),
                                           p.start or time(hour=0, minute=0)) + timedelta(hours=1)).replace(tzinfo=TZ)
            # if end_dt <= start_dt, bump end by one day to avoid zero/negative duration
            if end_dt <= start_dt:
                end_dt = end_dt + timedelta(days=1)
            events.append({
                "type": "timed",
                "dtstart": start_dt,
                "dtend": end_dt,
                "summary": p.summary,
                "url": p.url,
                "description": p.description
            })
        else:
            events.append({
                "type": "allday",
                "dtstart": p.dt,
                "dtend": p.dt + timedelta(days=1),
                "summary": p.summary,
                "url": p.url,
                "description": p.description
            })
    def sort_key(ev):
        k = ev["dtstart"]
        if isinstance(k, datetime):
            return k
        else:
            return datetime.combine(k, time.min).replace(tzinfo=TZ)
    events.sort(key=sort_key)
    return events

def format_dt_local(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")

def format_date(d: date) -> str:
    return d.strftime("%Y%m%d")

def filter_events_by_min_date(events: List[Dict], min_date: date) -> List[Dict]:
    """Keep only events whose start date (dtstart) is >= min_date (inclusive)."""
    out: List[Dict] = []
    for ev in events:
        sd = ev["dtstart"]
        if isinstance(sd, datetime):
            sd_date = sd.astimezone(TZ).date()
        else:
            sd_date = sd
        if sd_date >= min_date:
            out.append(ev)
    return out

def write_ics(events: List[Dict], outpath: str="calendar.ics"):
    now_utc = datetime.utcnow().replace(microsecond=0)
    dtstamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
    lines: List[str] = []
    lines.append("BEGIN:VCALENDAR")
    lines.append("PRODID:-//jeremytammik/jcal2//EN")
    lines.append("VERSION:2.0")
    lines.append("CALSCALE:GREGORIAN")
    lines.append("METHOD:PUBLISH")
    lines.append("X-WR-CALNAME:jeremy's calendar")
    lines.append("X-WR-TIMEZONE:Europe/Zurich")
    lines.append(VTIMEZONE_BLOCK.rstrip())
    lines.append(f"DTSTAMP:{dtstamp}")
    for ev in events:
        summary = ev.get("summary") or ""
        url = ev.get("url")
        desc = ev.get("description")
        # create a UID
        if ev["type"] == "timed":
            start = ev["dtstart"].astimezone(TZ)
            uid = f"{slugify(summary)}-{start.strftime('%Y%m%dT%H%M%S')}-{OWNER}"
        else:
            sd = ev["dtstart"]
            if isinstance(sd, date):
                uid = f"{slugify(summary)}-{sd.strftime('%Y%m%d')}-{OWNER}"
            else:
                uid = f"{slugify(summary)}-{sd.strftime('%Y%m%dT%H%M%S')}-{OWNER}"
        lines.append("")
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uid}")
        lines.append(f"DTSTAMP:{dtstamp}")
        if ev["type"] == "timed":
            dtstart = ev["dtstart"].astimezone(TZ)
            dtend = ev["dtend"].astimezone(TZ)
            lines.append(f"DTSTART;TZID=Europe/Zurich:{format_dt_local(dtstart)}")
            lines.append(f"DTEND;TZID=Europe/Zurich:{format_dt_local(dtend)}")
        else:
            lines.append(f"DTSTART;VALUE=DATE:{format_date(ev['dtstart'])}")
            lines.append(f"DTEND;VALUE=DATE:{format_date(ev['dtend'])}")
        lines.append(f"SUMMARY:{summary}")
        if desc:
            desc_esc = desc.replace("\n", "\\n")
            lines.append(f"DESCRIPTION:{desc_esc}")
        if url:
            lines.append(f"URL:{url}")
        lines.append("END:VEVENT")
    lines.append("")
    lines.append("END:VCALENDAR")
    Path(outpath).write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {outpath} ({len(events)} events)")

def main(argv):
    parser = argparse.ArgumentParser(description="Convert simple plaintext event list to ICS.")
    parser.add_argument("infile", help="Input file path (or '-' for stdin)")
    parser.add_argument("--min-date", "-m", dest="min_date", default=None,
                        help="Minimum ISO date (YYYY-MM-DD) for events to include. Defaults to today's date in Europe/Zurich.")
    parser.add_argument("--out", "-o", dest="out", default="calendar.ics", help="Output .ics path (default calendar.ics)")
    args = parser.parse_args(argv[1:])

    if args.infile == "-":
        raw_lines = sys.stdin.read().splitlines()
    else:
        input_file = Path(args.infile)
        if not input_file.exists():
            print("Input file not found:", input_file)
            sys.exit(2)
        raw_lines = input_file.read_text(encoding="utf-8").splitlines()

    parsed = parse_lines(raw_lines)
    events = group_multiday(parsed)

    # determine min_date default = today in Europe/Zurich
    if args.min_date:
        try:
            min_date = datetime.strptime(args.min_date, "%Y-%m-%d").date()
        except ValueError:
            print("Invalid --min-date format; expected YYYY-MM-DD")
            sys.exit(2)
    else:
        min_date = datetime.now(TZ).date()

    events = filter_events_by_min_date(events, min_date)
    write_ics(events, outpath=args.out)

if __name__ == "__main__":
    main(sys.argv)