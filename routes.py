"""The route-home half of Running Tab: TfL Journey Planner. Standard library only.

One request per venue:

    GET https://api.tfl.gov.uk/Journey/JourneyResults/{lat,lon}/to/{lat,lon}
        ?date=YYYYMMDD&time=HHMM&timeIs=Departing[&app_key=...]

Both ends are coordinates, worked out before this module is called (the
destination through the same Google lookup as the starting place), so TfL
never answers "300 Multiple Choices" for an ambiguous name.

TfL's times are London wall-clock times with no offset, so they are given
the Europe/London zone here before anything is returned.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from venues import LONDON, clock_label

JOURNEY_URL = "https://api.tfl.gov.uk/Journey/JourneyResults/{start}/to/{end}"

#: How a leg reads in a route summary, by TfL mode id.
_MODE_WORDS = {
    "tube": "{name} line",
    "bus": "{name} bus",
    "coach": "{name} coach",
    "dlr": "DLR",
    "elizabeth-line": "Elizabeth line",
    "overground": "{name}",
    "tram": "tram",
    "national-rail": "train ({name})",
    "river-bus": "river bus",
    "cable-car": "cable car",
}


def journey_url(start: tuple[float, float], end: tuple[float, float],
                depart: datetime, key: str = "") -> str:
    """The request for leaving `start` at `depart` (aware) for `end`.

    The address is fixed; only numbers we formatted ourselves go in the path,
    and the key goes only in the query, to this host.
    """
    local = depart.astimezone(LONDON)
    query = {"date": local.strftime("%Y%m%d"), "time": local.strftime("%H%M"),
             "timeIs": "Departing"}
    if key:
        query["app_key"] = key
    path = JOURNEY_URL.format(start=f"{start[0]:.5f},{start[1]:.5f}",
                              end=f"{end[0]:.5f},{end[1]:.5f}")
    return path + "?" + urlencode(query)


def _london(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=LONDON)
    return moment


def _leg_words(leg: dict) -> str:
    mode = ((leg.get("mode") or {}).get("id") or "").strip()
    if mode in ("walking", "cycle"):
        minutes = leg.get("duration")
        verb = "walk" if mode == "walking" else "cycle"
        if minutes == 0:
            return ""                   # "walk 0 min" says nothing
        return f"{verb} {minutes} min" if isinstance(minutes, int) else verb
    names = [r.get("name") for r in leg.get("routeOptions") or [] if r.get("name")]
    name = names[0] if names else ""
    pattern = _MODE_WORDS.get(mode)
    if pattern and (name or "{name}" not in pattern):
        return pattern.format(name=name)
    return name or mode.replace("-", " ") or "travel"


def _lines_used(journey: dict) -> list[str]:
    """Line names used in a journey, in order, e.g. ["14", "Victoria"]."""
    out = []
    for leg in journey.get("legs") or []:
        if ((leg.get("mode") or {}).get("id")) in ("walking", "cycle"):
            continue
        for option in leg.get("routeOptions") or []:
            name = (option.get("name") or "").strip()
            if name and name not in out:
                out.append(name)
    return out


def _disruptions(payload: dict, lines: list[str]) -> list[str]:
    """Current problems on the lines a journey uses, from TfL's `lines` list.

    "Good Service" is left out. The reason is TfL's own public status text
    (no key in it), shortened to keep the answer readable.
    """
    wanted = {name.lower() for name in lines}
    out = []
    for line in payload.get("lines") or []:
        name = (line.get("name") or "").strip()
        if name.lower() not in wanted and (line.get("id") or "").lower() not in wanted:
            continue
        for status in line.get("lineStatuses") or []:
            severity = (status.get("statusSeverityDescription") or "").strip()
            if not severity or severity == "Good Service":
                continue
            reason = " ".join((status.get("reason") or "").split())
            # TfL reasons often start "N205: " or "Victoria Line: " already.
            for prefix in (f"{name} line:", f"{name}:"):
                if reason.lower().startswith(prefix.lower()):
                    reason = reason[len(prefix):].strip()
                    break
            if len(reason) > 110:
                reason = reason[:107].rsplit(" ", 1)[0] + "..."
            text = f"{name}: {severity}" + (f" ({reason})" if reason else "")
            if text not in out:
                out.append(text)
    return out


def best_journey(payload: dict) -> dict | None:
    """The journey that gets home first, or None if TfL offered none.

    Earliest arrival, not shortest duration: a 43-minute trip that leaves
    twenty minutes later gets you home later than a 50-minute one.
    """
    candidates = []
    for journey in payload.get("journeys") or []:
        arrive = _london(journey.get("arrivalDateTime"))
        start = _london(journey.get("startDateTime"))
        if arrive is None or start is None:
            continue
        candidates.append((arrive, start, journey))
    if not candidates:
        return None
    arrive, start, journey = min(candidates, key=lambda c: (c[0], c[1]))
    legs = journey.get("legs") or []
    lines = _lines_used(journey)
    duration = journey.get("duration")
    if not isinstance(duration, int):
        duration = round((arrive - start).total_seconds() / 60)
    return {
        "start": start,
        "arrive": arrive,
        "minutes": duration,
        "summary": " > ".join(w for w in map(_leg_words, legs) if w) or "direct",
        "lines": lines,
        "disruptions": _disruptions(payload, lines),
    }


def describe(venue_name: str, depart: datetime, leave_now: bool, journey: dict) -> str:
    """"Luna Kensington: leave at 3am, home 3:59am (50 min from 3:09am): walk 4 min > ..."."""
    when = "leave now" if leave_now else f"leave at {clock_label(depart.astimezone(LONDON))}"
    start = journey["start"]
    waits = (start - depart).total_seconds() > 5 * 60
    trip = f"{journey['minutes']} min" + (f" from {clock_label(start)}" if waits else "")
    return (f"{venue_name}: {when}, home {clock_label(journey['arrive'])} "
            f"({trip}): {journey['summary']}")
