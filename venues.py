"""The part of Running Tab that knows about opening hours. Standard library only.

Kept apart from app.py so the hard bit — "when does this place next close?" —
can be read and tested on its own, with no SDK and no network.

The one rule everything here follows: **never compare bare clock times.**
"02:00" sorts before "23:00" as text and as a time of day, but a bar that
closes at 2am closes three hours *after* one that closes at 11pm. So every
closing time is turned into a full, timezone-aware datetime first, and only
those are compared.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

#: Google Places *primary* types searched for, with the word used for each.
#: Primary, not any type: Google tags restaurants, hotels, casinos and even a
#: dog daycare with `bar` or `cocktail_bar`, and a hotel's "open 24 hours" is
#: the front desk, not the bar. Restaurants are left out on purpose: most stop
#: serving long before they close.
TYPE_NAMES = {
    "bar": "bar",
    "pub": "pub",
    "night_club": "club",
    "wine_bar": "wine bar",
    "cocktail_bar": "cocktail bar",
    "lounge_bar": "bar",
    "sports_bar": "sports bar",
    "irish_pub": "pub",
    "gastropub": "pub",
    "brewpub": "brewpub",
    "beer_garden": "beer garden",
}
VENUE_TYPES = tuple(TYPE_NAMES)

#: Average walking speed used to turn "within 10 minutes' walk" into metres.
METRES_PER_WALK_MINUTE = 80

OPEN = "open"
OPEN_24H = "open_24h"
CLOSED = "closed"
UNKNOWN = "unknown"


# ── Where ────────────────────────────────────────────────────────────────────


def parse_latlon(text: str) -> tuple[float, float] | None:
    """`"51.4988, -0.1749"` -> (51.4988, -0.1749); anything else -> None."""
    parts = text.replace(" ", "").split(",")
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ── When ─────────────────────────────────────────────────────────────────────


def venue_zone(place: dict, now: datetime) -> tzinfo:
    """The venue's local time zone.

    Google gives only a UTC offset. When it matches London's offset at `now`
    the venue is taken to be in London, so a week that crosses a clock change
    is still right; anywhere else a fixed offset is the best available.
    """
    offset = place.get("utcOffsetMinutes")
    if offset is None:
        return LONDON
    london = now.astimezone(LONDON).utcoffset()
    if london is not None and london == timedelta(minutes=offset):
        return LONDON
    return timezone(timedelta(minutes=offset))


def _at(day: date, point: dict, zone: tzinfo) -> datetime:
    """A period's open/close point on a given date, as an aware datetime.

    Google uses hour 0-23, but an hour of 24 ("closes at 24:00") is treated
    as midnight at the end of that day rather than refused.
    """
    hour, minute = int(point.get("hour", 0)), int(point.get("minute", 0))
    extra = timedelta(0)
    if hour >= 24:
        hour, extra = hour - 24, timedelta(days=1)
    return datetime.combine(day + extra, time(hour, minute), tzinfo=zone)


def _dated_intervals(periods: list, zone: tzinfo) -> list[tuple[datetime, datetime | None]]:
    """Intervals from `currentOpeningHours`, whose points carry real dates."""
    out = []
    for period in periods:
        opening, closing = period.get("open"), period.get("close")
        if not opening or "date" not in opening:
            return []
        d = opening["date"]
        start = _at(date(d["year"], d["month"], d["day"]), opening, zone)
        # Current hours only cover a seven-day window. A period still running
        # at its end has a close marked `truncated` (a 24-hour bar "closes" at
        # 23:59 next Thursday), which is the window's edge, not a closing time.
        if not closing or closing.get("truncated"):
            out.append((start, None))
            continue
        if "date" not in closing:
            return []
        c = closing["date"]
        end = _at(date(c["year"], c["month"], c["day"]), closing, zone)
        out.append((start, end))
    return out


def _weekly_intervals(periods: list, local_now: datetime, zone: tzinfo
                      ) -> list[tuple[datetime, datetime | None]]:
    """Intervals from `regularOpeningHours`, laid out over three weeks.

    Google days are 0 = Sunday .. 6 = Saturday. A Friday 22:00 to Saturday
    04:00 period has its close on day 6, which is how the past-midnight case
    arrives: the close is computed from the open's date plus the number of
    days between the two, so it lands on the Saturday, after midnight.

    The week before and after are included so that "it is 1am on Sunday and
    the Saturday-night period is still running" and "the next week's periods"
    both exist to be found.
    """
    sunday = local_now.date() - timedelta(days=(local_now.weekday() + 1) % 7)
    out = []
    for week in (-1, 0, 1):
        base = sunday + timedelta(weeks=week)
        for period in periods:
            opening, closing = period.get("open"), period.get("close")
            if not opening:
                continue
            open_day = base + timedelta(days=int(opening.get("day", 0)))
            start = _at(open_day, opening, zone)
            if not closing:
                out.append((start, None))
                continue
            gap = (int(closing.get("day", 0)) - int(opening.get("day", 0))) % 7
            end = _at(open_day + timedelta(days=gap), closing, zone)
            if end <= start:            # closes at the same time a week later
                end += timedelta(weeks=1)
            out.append((start, end))
    return out


def _merge(intervals: list[tuple[datetime, datetime | None]]
           ) -> list[tuple[datetime, datetime | None]]:
    """Join intervals that touch or overlap.

    Some places are listed as "Mon 12:00-00:00" and "Tue 00:00-02:00": open
    straight through midnight, but as two periods. Merged, the close is 2am,
    not midnight.
    """
    merged: list[tuple[datetime, datetime | None]] = []
    for start, end in sorted(intervals, key=lambda i: i[0]):
        if merged:
            last_start, last_end = merged[-1]
            if last_end is None:
                continue
            if start <= last_end:
                if end is None or end > last_end:
                    merged[-1] = (last_start, end)
                continue
        merged.append((start, end))
    return merged


def next_close(place: dict, now: datetime) -> tuple[str, datetime | None]:
    """Is the place open at `now`, and if so, when does it next close?

    Returns one of:
      (OPEN, close_datetime)   open, closing at that moment (venue's local zone)
      (OPEN_24H, None)         open, and not closing within the next six days
      (CLOSED, None)           closed at `now`
      (UNKNOWN, None)          Google has no opening hours for it
    """
    zone = venue_zone(place, now)
    local_now = now.astimezone(zone)

    intervals: list[tuple[datetime, datetime | None]] = []
    current = (place.get("currentOpeningHours") or {}).get("periods") or []
    if current:
        # Current hours include bank holidays and one-off changes, so they win
        # when they cover `now`. They only reach back to today, so a moment
        # before their first period falls back to the regular week.
        dated = _dated_intervals(current, zone)
        if dated and min(s for s, _ in dated) <= local_now:
            intervals = dated
    if not intervals:
        regular = (place.get("regularOpeningHours") or {}).get("periods") or []
        if not regular:
            regular = current           # undated current hours, if that is all
        if not regular:
            return UNKNOWN, None
        intervals = _weekly_intervals(regular, local_now, zone)
        if not intervals:
            return UNKNOWN, None

    for start, end in _merge(intervals):
        if start <= local_now and (end is None or local_now < end):
            if end is None or end - local_now >= timedelta(days=6):
                return OPEN_24H, None
            return OPEN, end
    return CLOSED, None


_CLOCK = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.?|p\.m\.?)?$")
_WORDS = {"midnight": time(0, 0), "12 midnight": time(0, 0),
          "midday": time(12, 0), "noon": time(12, 0), "12 noon": time(12, 0)}


def parse_clock(text: str) -> time | None:
    """"2am", "1:30am", "1.30 am", "02:00", "23:30", "midnight" -> a time of day.

    A bare hour with no am/pm is read the way people mean it of a night out:
    "till 2" is 2am, "till 11" is 11pm, "till 12" is midnight. Written with a
    leading zero or past 12 ("02:00", "23:00") it is a 24-hour clock.
    Anything else -> None.
    """
    text = " ".join(text.lower().replace("o'clock", "").split())
    if text in _WORDS:
        return _WORDS[text]
    match = _CLOCK.match(text)
    if not match:
        return None
    digits, minute_text, suffix = match.groups()
    hour, minute = int(digits), int(minute_text or 0)
    if minute > 59:
        return None
    if suffix:
        if not 1 <= hour <= 12:
            return None
        hour %= 12
        if suffix.startswith("p"):
            hour += 12
    elif hour > 23:
        return None
    elif hour <= 12 and not digits.startswith("0"):
        if hour == 12:
            hour = 0                    # "till 12" on a night out is midnight
        elif hour >= 7:
            hour += 12                  # "till 11" is 11pm; "till 2" is 2am
    return time(hour, minute)


def tonight_at(clock: time, now: datetime, zone: tzinfo = LONDON) -> datetime:
    """The moment `clock` refers to, said at `now`: past midnight aware.

    The next time the clock shows it, unless it showed it in the last six
    hours. So at 11pm on Friday, "2am" is Saturday 2am and "11pm" is the 11pm
    just gone (every place open now is open after it); at 10am, "midnight" is
    tonight's.
    """
    local_now = now.astimezone(zone)
    earliest = local_now - timedelta(hours=6)
    moment = datetime.combine(earliest.date(), clock, tzinfo=zone)
    if moment < earliest:
        moment += timedelta(days=1)
    return moment


def open_until(entries: list, moment: datetime) -> list:
    """The entries still open at `moment`: closing then or later, or 24 hours."""
    return [v for v in entries
            if v["status"] == OPEN_24H or v["closes_at"] is None or v["closes_at"] >= moment]


def clock_label(moment: datetime) -> str:
    """`01:00` -> "1am", `00:30` -> "12:30am", `00:00` -> "midnight".

    Built by hand: `%-I` is a GNU extension that raises on Windows.
    """
    if moment.hour == 0 and moment.minute == 0:
        return "midnight"
    if moment.hour == 12 and moment.minute == 0:
        return "midday"
    hour = moment.hour % 12 or 12
    suffix = "am" if moment.hour < 12 else "pm"
    if moment.minute:
        return f"{hour}:{moment.minute:02d}{suffix}"
    return f"{hour}{suffix}"


def close_label(status: str, close: datetime | None, now: datetime) -> str:
    """"open till 1am", "open till 11pm Saturday", "open 24 hours"."""
    if status == OPEN_24H or close is None:
        return "open 24 hours"
    label = f"open till {clock_label(close)}"
    local_now = now.astimezone(close.tzinfo)
    # A close in the small hours after tonight, or later the same day, needs
    # no day name; anything further off does ("till 2am Saturday" from a
    # Thursday afternoon is a different promise to "till 2am").
    hours_away = (close - local_now).total_seconds() / 3600
    if hours_away > 20:
        label += " " + close.strftime("%A")
    return label


# ── Ranking ──────────────────────────────────────────────────────────────────


def place_name(place: dict) -> str:
    return ((place.get("displayName") or {}).get("text") or "").strip()


def rank(places: list, now: datetime, centre: tuple[float, float]) -> dict:
    """Sort the open places by how late they stay open, latest first,
    with open-24-hours places after every place that has a closing time.

    Returns {"open": [entries...], "unknown": [names...], "closed": count},
    where each entry is a dict with name, kind, distance_m, point (lat, lon),
    status, closes_at.
    Places that have shut for good, or for now, are dropped altogether.
    """
    open_places, unknown, closed = [], [], 0
    for place in places:
        name = place_name(place)
        if not name:
            continue
        if place.get("businessStatus") in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"):
            continue
        status, close = next_close(place, now)
        kind = TYPE_NAMES.get(place.get("primaryType") or "")
        if kind is None:                # not primarily somewhere to drink
            continue
        if status == UNKNOWN:
            unknown.append(name)
            continue
        if status == CLOSED:
            closed += 1
            continue
        loc = place.get("location") or {}
        metres, point = None, None
        if "latitude" in loc and "longitude" in loc:
            point = (loc["latitude"], loc["longitude"])
            metres = distance_m(centre[0], centre[1], point[0], point[1])
        open_places.append({
            "name": name,
            "kind": kind,
            "distance_m": metres,
            "point": point,
            "status": status,
            "closes_at": close,
        })

    # Latest close first (full datetimes, so 1am Saturday beats 11pm Friday),
    # nearest first to break a tie, and open-24-hours places last: they are
    # mostly hotel lounges, and "open till 3am" is the answer people want.
    open_places.sort(key=lambda v: (
        v["status"] == OPEN_24H,
        -v["closes_at"].timestamp() if v["closes_at"] else 0.0,
        v["distance_m"] if v["distance_m"] is not None else math.inf,
    ))
    return {"open": open_places, "unknown": unknown, "closed": closed}


def distance_label(metres: float | None) -> str:
    if metres is None:
        return "distance unknown"
    minutes = max(1, round(metres / METRES_PER_WALK_MINUTE))
    return f"{round(metres / 10) * 10:.0f} m, about {minutes} min walk"


def describe(entry: dict, now: datetime) -> str:
    """One line per venue, the form the agent reads."""
    return (f"{entry['name']} ({entry['kind']}, {distance_label(entry['distance_m'])}): "
            f"{close_label(entry['status'], entry['closes_at'], now)}")
