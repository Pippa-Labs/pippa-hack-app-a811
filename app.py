"""Running Tab: where can I still get a drink near here, and get home after?

Google Places (New), POSTs to a fixed address:

  1. Text Search   turns the place the person named into coordinates
                   (skipped when they gave "lat, lon"), and the destination
                   too, when there is one.
  2. Nearby Search finds bars, pubs and clubs within walking distance,
                   with their opening hours.

TfL Journey Planner, GETs to a fixed address, only when a destination is
given: one per venue for the top five, in parallel, departing at each
venue's closing time.

The opening-hours logic lives in venues.py and the TfL parsing in routes.py.
This file is the Pippa side: inputs in, errors as sentences, data in the
shape manifest.yaml declares.

The keys: GOOGLE_MAPS_API_KEY, sent only to places.googleapis.com in a
header that does not follow redirects; TFL_APP_KEY (optional), sent only to
api.tfl.gov.uk in the query. Both are read from os.environ inside handle().
Nothing from the question chooses a host, a path or a header. No request
follows a redirect.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    RateLimitedError,
    Result,
    ServiceUnavailableError,
    load_manifest_file,
    validate_manifest,
)

import routes
import venues

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")

USER_AGENT = "pippa-agent-app-running-tab/0.1 (https://github.com/Pippa-Labs)"

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"

TEXT_SEARCH_FIELDS = "places.displayName,places.formattedAddress,places.location"
NEARBY_FIELDS = ",".join([
    "places.id",
    "places.displayName",
    "places.location",
    "places.primaryType",
    "places.businessStatus",
    "places.utcOffsetMinutes",
    "places.currentOpeningHours",
    "places.regularOpeningHours",
])

#: Great Britain and Northern Ireland, roughly. Places outside are not found.
UK_BOUNDS = {"low": {"latitude": 49.8, "longitude": -8.7},
             "high": {"latitude": 60.9, "longitude": 1.8}}

DEFAULT_WALK_MINUTES = 10
MAX_WALK_MINUTES = 30
#: How many venues go back to Pippa. Google returns at most 20.
MAX_VENUES = 8
#: How many of them get a route home (one TfL request each, in parallel).
MAX_ROUTES = 5
#: Seconds of Pippa's 20 that this app plans to use, leaving headroom.
BUDGET_SECONDS = 17.0
NO_ROUTE = "no route found at that time"


class KeyMissing(AgentAppError):
    user_message = "Running Tab isn't set up yet: its builder still has to add its Google Maps key."
    retryable = False


class GoogleRefused(AgentAppError):
    user_message = ("Google Maps refused Running Tab's request, so its builder needs to "
                    "check the app's Google Maps key.")
    retryable = False


class NoLocation(AgentAppError):
    user_message = ("Tell me where you are (an area, a street, a station or a postcode) "
                    "and I'll find what's open latest nearby.")
    retryable = False


class PlaceNotFound(AgentAppError):
    user_message = "I could not find that place."
    retryable = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: the key never goes anywhere but Google."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class App:
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    # ── The one method that touches the network ──────────────────────────────

    def _post(self, url: str, body: dict, field_mask: str, key: str,
              timeout: float = 6.0) -> bytes:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "X-Goog-FieldMask": field_mask,
            },
        )
        request.add_unredirected_header("X-Goog-Api-Key", key)
        with _OPENER.open(request, timeout=timeout) as response:
            return response.read()

    def _get(self, url: str, timeout: float = 6.0) -> bytes:
        """GET for TfL. The key, if any, is already in the query of `url`."""
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with _OPENER.open(request, timeout=timeout) as response:
            return response.read()

    def _google(self, url: str, body: dict, field_mask: str, key: str,
                what: str, timeout: float) -> dict:
        """POST to Google and return the JSON, with every failure a sentence.

        Never passes on Google's error text or the URL: say what happened
        from the status code alone.
        """
        try:
            raw = self._post(url, body, field_mask, key, timeout)
        except urllib.error.HTTPError as exc:       # first: it IS a URLError
            if exc.code in (401, 403):
                raise GoogleRefused(f"{what}: google answered {exc.code}")
            if exc.code == 429:
                raise RateLimitedError(f"{what}: google answered 429")
            raise ServiceUnavailableError(f"{what}: google answered {exc.code}")
        except TimeoutError:                        # NOT a URLError
            raise ServiceUnavailableError(f"{what}: google did not answer in time")
        except urllib.error.URLError as exc:
            raise ServiceUnavailableError(f"{what}: could not reach google ({exc.reason})")
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            raise ServiceUnavailableError(f"{what}: google sent something that is not JSON")
        return payload if isinstance(payload, dict) else {}

    # ── The outcome ──────────────────────────────────────────────────────────

    def handle(self, call: Call) -> Result:
        started = time.monotonic()
        if call.outcome_id != "venues.latest":
            raise AgentAppError(f"this app cannot do {call.outcome_id!r}")

        location = " ".join((call.inputs.get("location") or "").split())
        if not location:
            raise NoLocation("no location in the message")
        destination = " ".join((call.inputs.get("destination") or "").split())

        key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
        if not key:
            raise KeyMissing("GOOGLE_MAPS_API_KEY is not set: .env locally, Keys in Pippa")

        walk = call.inputs.get("walk_minutes")
        if not isinstance(walk, int) or isinstance(walk, bool) or walk <= 0:
            walk = DEFAULT_WALK_MINUTES
        walk = min(walk, MAX_WALK_MINUTES)
        radius = walk * venues.METRES_PER_WALK_MINUTE

        lat, lon, location_used = self._locate(location, key)

        found = self._google(
            NEARBY_SEARCH_URL,
            {
                "includedPrimaryTypes": list(venues.VENUE_TYPES),
                "maxResultCount": 20,
                "rankPreference": "DISTANCE",
                "locationRestriction": {
                    "circle": {"center": {"latitude": lat, "longitude": lon},
                               "radius": float(radius)},
                },
            },
            NEARBY_FIELDS, key, "nearby search", timeout=5.0,
        )
        places = found.get("places") or []

        now = call.context.now
        ranked = venues.rank(places, now, (lat, lon))
        top = ranked["open"][:MAX_VENUES]

        data = {
            "location_used": location_used,
            "search_radius_m": radius,
            "venues": [venues.describe(v, now) for v in top],
            "open_count": len(ranked["open"]),
            "closed_count": ranked["closed"],
            "unknown_hours": ranked["unknown"][:MAX_VENUES],
        }
        if top:
            data["latest_venue"] = top[0]["name"]
            data["latest_closing"] = venues.close_label(
                top[0]["status"], top[0]["closes_at"], now)
            if top[0]["closes_at"] is not None:
                # Aware (venue's own zone), so it carries a UTC offset.
                data["latest_closes_at"] = top[0]["closes_at"].isoformat()

        first_route = None
        if destination:
            first_route = self._routes_home(top, destination, key, now, started, data)

        return Result(data=data, narrative=self._sentence(
            top, location_used, walk, now, first_route, data.get("destination_used")))

    # ── Where ────────────────────────────────────────────────────────────────

    def _locate(self, location: str, key: str) -> tuple[float, float, str]:
        """Coordinates for what the person typed, and the name we used."""
        coords = venues.parse_latlon(location)
        if coords:
            return coords[0], coords[1], location

        found = self._google(
            TEXT_SEARCH_URL,
            {"textQuery": location, "regionCode": "GB", "maxResultCount": 1,
             # UK only: Google's text search is generous enough to turn
             # nonsense into a bar in Bangkok, and the routes are TfL's.
             "locationRestriction": {"rectangle": UK_BOUNDS}},
            TEXT_SEARCH_FIELDS, key, "text search", timeout=4.0,
        )
        places = found.get("places") or []      # 200 with {} for no match
        point = (places[0].get("location") or {}) if places else {}
        if "latitude" not in point or "longitude" not in point:
            error = PlaceNotFound(f"text search found nothing for {location!r}")
            error.user_message = f"I could not find anywhere in the UK called {location}."
            raise error
        name = (places[0].get("formattedAddress")
                or venues.place_name(places[0]) or location)
        return point["latitude"], point["longitude"], name

    # ── Home ─────────────────────────────────────────────────────────────────

    def _routes_home(self, top: list, destination: str, key: str, now: datetime,
                     started: float, data: dict) -> dict | None:
        """Fill in the route fields of `data`; return the first venue's journey.

        A route home is extra: nothing here raises. If the destination cannot
        be found or TfL fails, the venues still go back, with `route_note`
        saying why there are no routes.
        """
        if not top:
            return None
        try:
            end_lat, end_lon, end_name = self._locate(destination, key)
        except PlaceNotFound:
            data["route_note"] = f"I could not find {destination} in the UK, so there are no routes home."
            return None
        except AgentAppError:
            data["route_note"] = "Google Maps could not look up the destination just now, so there are no routes home."
            return None
        data["destination_used"] = end_name

        remaining = BUDGET_SECONDS - (time.monotonic() - started)
        if remaining < 2.0:
            data["route_note"] = "There was no time left to plan routes home; ask again for them."
            return None
        timeout = min(6.0, remaining - 0.5)
        tfl_key = os.environ.get("TFL_APP_KEY", "")

        jobs = []
        for venue in top[:MAX_ROUTES]:
            leave_now = venue["closes_at"] is None
            depart = now if leave_now else venue["closes_at"]
            jobs.append((venue, depart, leave_now))

        def plan(job):
            venue, depart, _ = job
            if venue["point"] is None:
                return None, "no location for this venue"
            url = routes.journey_url(venue["point"], (end_lat, end_lon), depart, tfl_key)
            return self._journey(url, timeout)

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            results = list(pool.map(plan, jobs))

        lines, disruptions, problems = [], [], []
        first = None
        for (venue, depart, leave_now), (journey, problem) in zip(jobs, results):
            if journey is None:
                lines.append(f"{venue['name']}: {problem}")
                problems.append(problem)
                continue
            lines.append(routes.describe(venue["name"], depart, leave_now, journey))
            for item in journey["disruptions"]:
                if item not in disruptions:
                    disruptions.append(item)
            if venue is top[0]:
                first = journey
                data["latest_venue_home_at"] = journey["arrive"].isoformat()
                data["latest_venue_journey_min"] = journey["minutes"]
                data["latest_venue_route"] = journey["summary"]

        data["routes_home"] = lines
        data["route_disruptions"] = disruptions
        if problems and len(problems) == len(jobs):
            if all(p == NO_ROUTE for p in problems):
                data["route_note"] = (f"TfL found no routes from these venues to {end_name}; "
                                      "it plans journeys in and around London only.")
            else:
                data["route_note"] = f"No routes home: {problems[0]}."
        return first

    def _journey(self, url: str, timeout: float) -> tuple[dict | None, str | None]:
        """One TfL request: (journey, None) or (None, what went wrong).

        Never passes on TfL's error text or the URL (it carries the key).
        """
        try:
            raw = self._get(url, timeout)
        except urllib.error.HTTPError as exc:       # first: it IS a URLError
            if exc.code in (400, 404):
                return None, NO_ROUTE
            if exc.code == 429:
                return None, "TfL is busy, try again shortly"
            if exc.code in (401, 403):
                return None, "TfL refused the request"
            return None, "TfL's journey planner is not answering"
        except TimeoutError:                        # NOT a URLError
            return None, "TfL did not answer in time"
        except (urllib.error.URLError, OSError):
            return None, "TfL could not be reached"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return None, "TfL sent something unreadable"
        journey = routes.best_journey(payload if isinstance(payload, dict) else {})
        if journey is None:
            return None, NO_ROUTE
        return journey, None

    # ── The sentence ─────────────────────────────────────────────────────────

    @staticmethod
    def _sentence(top: list, location_used: str, walk: int, now,
                  route: dict | None = None, destination: str | None = None) -> str:
        if not top:
            return (f"Nothing within about {walk} minutes' walk of {location_used} "
                    f"is open right now, as far as Google's opening hours say.")
        first = top[0]
        lead = (f"Open latest near {location_used}: {first['name']}, "
                f"{venues.close_label(first['status'], first['closes_at'], now)}")
        rest = [f"{v['name']} {venues.close_label(v['status'], v['closes_at'], now)}"
                for v in top[1:3]]
        text = lead + (". Then " + "; ".join(rest) + "." if rest else ".")
        if route is not None:
            when = ("leaving now" if first["closes_at"] is None
                    else f"leaving at {venues.clock_label(first['closes_at'])}")
            text += (f" From there, {when}, you'd reach {destination} at "
                     f"{venues.clock_label(route['arrive'])} ({route['minutes']} min: "
                     f"{route['summary']}).")
        return text
