"""Tests for Running Tab, run through LocalRunner so the manifest is enforced.

The network is blocked (conftest.py). Google is replaced by stubbing
`App._post` and TfL by stubbing `App._get`, the two methods that do HTTP.
The payloads below have the same shape as real Places API (New) and TfL
Journey Planner responses captured on 20 Nov 2026.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from pippa_agentapp_sdk import LocalRunner, RateLimitedError, ServiceUnavailableError

import routes
import venues
from app import (
    NEARBY_SEARCH_URL,
    TEXT_SEARCH_URL,
    App,
    BadTime,
    GoogleRefused,
    KeyMissing,
    NoLocation,
    PlaceNotFound,
)

FAKE_KEY = "test-key-not-real-000000000000"  # pragma: allowlist secret
FAKE_TFL_KEY = "tfl-test-key-not-real-00000"  # pragma: allowlist secret

# Friday 20 November 2026, 23:00 in London (GMT, so UTC+0).
FRI_2300 = datetime(2026, 11, 20, 23, 0, tzinfo=timezone.utc)
SAT_0130 = datetime(2026, 11, 21, 1, 30, tzinfo=timezone.utc)
SUN_0100 = datetime(2026, 11, 22, 1, 0, tzinfo=timezone.utc)

CENTRE = {"latitude": 51.4941, "longitude": -0.1738}
HOME = {"latitude": 51.5361, "longitude": -0.1225}     # N1 9GU


def weekly(*spans):
    """regularOpeningHours from (open_day, "HH:MM", close_day, "HH:MM"), 0 = Sunday."""
    periods = []
    for open_day, open_at, close_day, close_at in spans:
        oh, om = map(int, open_at.split(":"))
        ch, cm = map(int, close_at.split(":"))
        periods.append({"open": {"day": open_day, "hour": oh, "minute": om},
                        "close": {"day": close_day, "hour": ch, "minute": cm}})
    return {"periods": periods}


def every_day(open_at, close_at, overnight=False):
    return weekly(*[(d, open_at, (d + 1) % 7 if overnight else d, close_at)
                    for d in range(7)])


def place(name, hours=None, kind="pub", lat=51.4950, lon=-0.1740, **extra):
    p = {"id": name.lower().replace(" ", "-"), "displayName": {"text": name},
         "primaryType": kind, "location": {"latitude": lat, "longitude": lon},
         "utcOffsetMinutes": 0, "businessStatus": "OPERATIONAL"}
    if hours is not None:
        p["regularOpeningHours"] = hours
    p.update(extra)
    return p


PUB_2300 = place("The Early Pub", every_day("11:00", "23:00"))
PUB_MIDNIGHT = place("Zetland Arms", every_day("09:00", "00:00", overnight=True))
# Fri 22:00 -> Sat 03:00 and Sat 22:00 -> Sun 03:00: the close is on the NEXT day.
CLUB_3AM = place("Luna Kensington", weekly((5, "22:00", 6, "03:00"), (6, "22:00", 0, "03:00")),
                 kind="night_club", lat=51.4960)
NO_HOURS = place("Teahouse", None, kind="cocktail_bar")


class FakeGoogle:
    """Stands in for App._post and records what it was asked."""

    def __init__(self, nearby=None, text=None, error=None, dest=None):
        self.nearby = {"places": nearby or []}
        self.text = text if text is not None else {
            "places": [{"displayName": {"text": "South Kensington"},
                        "formattedAddress": "South Kensington, London",
                        "location": CENTRE}]}
        #: What a text search for the destination returns ("N1 9GU").
        self.dest = dest if dest is not None else {
            "places": [{"formattedAddress": "90 York Wy, London N1 9GU", "location": HOME}]}
        self.error = error
        self.calls = []

    def __call__(self, url, body, field_mask, key, timeout=6.0):
        self.calls.append({"url": url, "body": body, "key": key})
        if self.error is not None:
            raise self.error
        if url == TEXT_SEARCH_URL:
            payload = self.dest if body["textQuery"] == "N1 9GU" else self.text
        else:
            payload = self.nearby
        return json.dumps(payload).encode("utf-8")


def tfl_journey(start, arrive, legs):
    """One TfL journey; times are naive London wall-clock, as TfL sends them."""
    s, a = datetime.fromisoformat(start), datetime.fromisoformat(arrive)
    return {"startDateTime": start, "arrivalDateTime": arrive,
            "duration": int((a - s).total_seconds() // 60), "legs": legs}


def leg(mode, minutes, name=""):
    return {"mode": {"id": mode}, "duration": minutes,
            "instruction": {"summary": f"{name} {mode}"},
            "routeOptions": [{"name": name}]}


#: Shaped on the live 03:00 South Kensington -> N1 9GU answer.
NIGHT_ROUTE = {
    "journeys": [
        # 43 min, but leaves later and gets home later: not the one to pick.
        tfl_journey("2026-11-21T03:29:00", "2026-11-21T04:12:00",
                    [leg("walking", 4), leg("bus", 24, "14"), leg("bus", 9, "N91"),
                     leg("walking", 3)]),
        tfl_journey("2026-11-21T03:09:00", "2026-11-21T03:59:00",
                    [leg("walking", 0), leg("walking", 4), leg("bus", 8, "14"),
                     leg("tube", 7, "Victoria"), leg("walking", 15)]),
    ],
    "lines": [
        {"id": "14", "name": "14", "lineStatuses": [{"statusSeverityDescription": "Good Service"}]},
        {"id": "victoria", "name": "Victoria", "lineStatuses": [{
            "statusSeverityDescription": "Severe Delays",
            "reason": "Victoria Line: Severe delays due to an earlier faulty train."}]},
        {"id": "n91", "name": "N91", "lineStatuses": [{
            "statusSeverityDescription": "Special Service",
            "reason": "SOUTHAMPTON ROW: on diversion."}]},
    ],
}


class FakeTfl:
    """Stands in for App._get. `answer` is a payload, an exception, or a
    function of the parsed query returning either."""

    def __init__(self, answer=None):
        self.answer = NIGHT_ROUTE if answer is None else answer
        self.urls = []

    def __call__(self, url, timeout=6.0):
        self.urls.append(url)
        answer = self.answer(parse_qs(urlsplit(url).query)) if callable(self.answer) else self.answer
        if isinstance(answer, BaseException):
            raise answer
        return json.dumps(answer).encode("utf-8")


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("TFL_APP_KEY", FAKE_TFL_KEY)


def run(google, inputs=None, now=FRI_2300, explicit=False, tfl=None):
    app = App()
    app._post = google
    app._get = tfl if tfl is not None else FakeTfl()
    if inputs is None:
        inputs = {"location": "South Kensington"}
    return LocalRunner(app).call(
        "venues.latest", inputs, granted_secrets={}, now=now,
        request_id="t1", explicit=explicit,
    ).result


# ── The manifest ─────────────────────────────────────────────────────────────


def test_the_manifest_is_valid():
    app = App()
    assert app.manifest.app_id == "hack.app_a811"
    assert [o.id for o in app.manifest.outcomes] == ["venues.latest"]


def test_the_summary_never_contains_by():
    """The badge is parsed from "[via <name> <version> by <author>]"."""
    assert " by " not in f" {App().manifest.summary} ".lower()


# ── Closing times, the point of the app ──────────────────────────────────────


def test_a_past_midnight_close_ranks_above_an_earlier_one():
    """3am Saturday is later than midnight, which is later than 11pm.

    Compared as clock times, "03:00" < "23:00" and the club would come last.
    """
    data = run(FakeGoogle([PUB_2300, PUB_MIDNIGHT, CLUB_3AM]),
               now=datetime(2026, 11, 20, 22, 30, tzinfo=timezone.utc)).data
    names = [line.split(" (")[0] for line in data["venues"]]
    assert names == ["Luna Kensington", "Zetland Arms", "The Early Pub"]
    assert data["latest_venue"] == "Luna Kensington"
    assert data["latest_closing"] == "open till 3am"
    assert data["latest_closes_at"] == "2026-11-21T03:00:00+00:00"


def test_after_midnight_only_the_late_ones_are_open():
    """Saturday 01:30: the Friday-night club period is still running."""
    data = run(FakeGoogle([PUB_2300, PUB_MIDNIGHT, CLUB_3AM]), now=SAT_0130).data
    assert len(data["venues"]) == 1
    assert data["venues"][0].startswith("Luna Kensington (club, ")
    assert data["venues"][0].endswith("walk): open till 3am")
    assert data["open_count"] == 1
    assert data["closed_count"] == 2


def test_the_week_wraps_from_saturday_night_into_sunday():
    """Sat 22:00 -> Sun 03:00 is Google day 6 -> day 0, across the week's end."""
    data = run(FakeGoogle([CLUB_3AM]), now=SUN_0100).data
    assert data["latest_closes_at"] == "2026-11-22T03:00:00+00:00"


def test_three_am_with_nothing_open_says_so():
    result = run(FakeGoogle([PUB_2300, PUB_MIDNIGHT, CLUB_3AM]),
                 now=datetime(2026, 11, 21, 3, 30, tzinfo=timezone.utc))
    assert result.data["venues"] == []
    assert result.data["open_count"] == 0
    assert "latest_venue" not in result.data
    assert "is open right now" in result.narrative


def test_midnight_split_into_two_periods_is_one_late_night():
    """"Fri 12:00-00:00" plus "Sat 00:00-02:00" closes at 2am, not midnight."""
    bar = place("Split Bar", weekly((5, "12:00", 6, "00:00"), (6, "00:00", 6, "02:00")),
                kind="bar")
    data = run(FakeGoogle([bar])).data
    assert data["latest_closing"] == "open till 2am"


ALLDAY = {"periods": [{"open": {"day": 0, "hour": 0, "minute": 0}}]}


def test_open_24_hours_ranks_after_places_that_close():
    allday = place("Billionaire Lounge", ALLDAY, kind="lounge_bar")
    data = run(FakeGoogle([allday, PUB_MIDNIGHT, CLUB_3AM])).data
    names = [line.split(" (")[0] for line in data["venues"]]
    assert names == ["Luna Kensington", "Zetland Arms", "Billionaire Lounge"]
    assert data["latest_venue"] == "Luna Kensington"
    assert data["venues"][-1].endswith(": open 24 hours")


def test_open_24_hours_alone_is_still_the_answer():
    allday = place("Billionaire Lounge", ALLDAY, kind="lounge_bar")
    data = run(FakeGoogle([allday])).data
    assert data["latest_venue"] == "Billionaire Lounge"
    assert data["latest_closing"] == "open 24 hours"
    assert "latest_closes_at" not in data


def test_current_hours_win_over_regular_ones():
    """A one-off early close (from currentOpeningHours, with real dates)."""
    early = place("Holiday Pub", every_day("09:00", "00:00", overnight=True),
                  currentOpeningHours={"periods": [{
                      "open": {"day": 5, "hour": 9, "minute": 0,
                               "date": {"year": 2026, "month": 11, "day": 20}},
                      "close": {"day": 5, "hour": 23, "minute": 30,
                                "date": {"year": 2026, "month": 11, "day": 20}}}]})
    data = run(FakeGoogle([early])).data
    assert data["latest_closing"] == "open till 11:30pm"


def test_a_truncated_close_is_the_window_edge_not_a_closing_time():
    """Captured from a real 24-hour bar: current hours "close" at 23:59 next Thursday."""
    allday = place("Glamis Bar", {"periods": [{"open": {"day": 0, "hour": 0, "minute": 0}}]},
                   kind="bar", currentOpeningHours={"periods": [{
                       "open": {"day": 5, "hour": 0, "minute": 0, "truncated": True,
                                "date": {"year": 2026, "month": 11, "day": 20}},
                       "close": {"day": 4, "hour": 23, "minute": 59, "truncated": True,
                                 "date": {"year": 2026, "month": 11, "day": 26}}}]})
    data = run(FakeGoogle([allday]), now=SAT_0130).data
    assert data["latest_closing"] == "open 24 hours"


def test_summer_time_closes_carry_the_bst_offset():
    july_fri_2300_bst = datetime(2026, 7, 17, 22, 0, tzinfo=timezone.utc)
    club = dict(CLUB_3AM, utcOffsetMinutes=60)
    data = run(FakeGoogle([club]), now=july_fri_2300_bst).data
    assert data["latest_closes_at"] == "2026-07-18T03:00:00+01:00"


def test_a_close_days_away_names_the_day():
    thursday_afternoon = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    weekender = place("Weekend Bar", weekly((4, "12:00", 6, "02:00")), kind="bar")
    data = run(FakeGoogle([weekender]), now=thursday_afternoon).data
    assert data["latest_closing"] == "open till 2am Saturday"


# ── What is and is not a venue ───────────────────────────────────────────────


def test_unknown_hours_are_listed_not_ranked():
    data = run(FakeGoogle([PUB_MIDNIGHT, NO_HOURS])).data
    assert data["unknown_hours"] == ["Teahouse"]
    assert len(data["venues"]) == 1


def test_hotels_and_restaurants_are_left_out():
    """A hotel's "open 24 hours" is its front desk, not its bar."""
    hotel = place("The Rembrandt Hotel", {"periods": [{"open": {"day": 0, "hour": 0, "minute": 0}}]},
                  kind="hotel")
    diner = place("Bella Italia", every_day("11:00", "23:30"), kind="restaurant")
    data = run(FakeGoogle([hotel, diner, PUB_MIDNIGHT])).data
    assert [v.split(" (")[0] for v in data["venues"]] == ["Zetland Arms"]


def test_permanently_closed_places_are_dropped():
    gone = place("Old Pub", every_day("11:00", "23:00"), businessStatus="CLOSED_PERMANENTLY")
    data = run(FakeGoogle([gone, PUB_2300]), now=datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)).data
    assert data["open_count"] == 1
    assert data["closed_count"] == 0


def test_clock_labels():
    t = lambda h, m: datetime(2026, 11, 20, h, m, tzinfo=timezone.utc)
    assert venues.clock_label(t(0, 0)) == "midnight"
    assert venues.clock_label(t(0, 30)) == "12:30am"
    assert venues.clock_label(t(1, 0)) == "1am"
    assert venues.clock_label(t(23, 30)) == "11:30pm"
    assert venues.clock_label(t(12, 0)) == "midday"


# ── Where, and the requests sent ─────────────────────────────────────────────


def test_a_place_name_is_looked_up_then_searched_around():
    google = FakeGoogle([PUB_MIDNIGHT])
    data = run(google).data
    assert data["location_used"] == "South Kensington, London"
    assert data["search_radius_m"] == 800
    assert [c["url"] for c in google.calls] == [TEXT_SEARCH_URL, NEARBY_SEARCH_URL]
    circle = google.calls[1]["body"]["locationRestriction"]["circle"]
    assert circle["center"] == CENTRE
    # The key goes to Google's fixed address and nowhere else.
    assert all(c["key"] == FAKE_KEY for c in google.calls)


def test_coordinates_skip_the_lookup():
    google = FakeGoogle([PUB_MIDNIGHT])
    data = run(google, {"location": "51.4941, -0.1738"}).data
    assert [c["url"] for c in google.calls] == [NEARBY_SEARCH_URL]
    assert data["location_used"] == "51.4941, -0.1738"


def test_walk_minutes_sets_the_radius_with_a_cap():
    google = FakeGoogle([])
    assert run(google, {"location": "Soho", "walk_minutes": 5}).data["search_radius_m"] == 400
    assert run(google, {"location": "Soho", "walk_minutes": 90}).data["search_radius_m"] == 2400


def test_a_place_google_cannot_find_is_named():
    """Google answers 200 with {} when nothing matches."""
    with pytest.raises(PlaceNotFound) as failure:
        run(FakeGoogle(text={}), {"location": "Narnia"})
    assert failure.value.user_message == "I could not find anywhere in the UK called Narnia."


# ── Refusals and failures ────────────────────────────────────────────────────


def test_no_location_asks_for_one():
    with pytest.raises(NoLocation):
        run(FakeGoogle(), {"location": ""})


def test_picked_by_name_it_answers_and_refuses_as_usual():
    picked = run(FakeGoogle([PUB_MIDNIGHT]), explicit=True)
    assert picked.data == run(FakeGoogle([PUB_MIDNIGHT])).data
    with pytest.raises(NoLocation):
        run(FakeGoogle(), {}, explicit=True)


def test_a_missing_key_is_said_plainly(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    with pytest.raises(KeyMissing):
        run(FakeGoogle())


def _http(code):
    return urllib.error.HTTPError(NEARBY_SEARCH_URL, code, "error", {}, None)


def test_a_refused_key_is_the_builders_problem():
    with pytest.raises(GoogleRefused):
        run(FakeGoogle(error=_http(403)))


def test_rate_limited_is_retryable():
    with pytest.raises(RateLimitedError) as failure:
        run(FakeGoogle(error=_http(429)))
    assert failure.value.retryable is True


def test_a_server_error_is_unavailable():
    with pytest.raises(ServiceUnavailableError):
        run(FakeGoogle(error=_http(500)))


def test_a_timeout_is_caught():
    """TimeoutError is not a URLError, so it needs its own clause."""
    with pytest.raises(ServiceUnavailableError):
        run(FakeGoogle(error=TimeoutError("read timed out")))


def test_an_unreachable_host_is_caught():
    with pytest.raises(ServiceUnavailableError):
        run(FakeGoogle(error=urllib.error.URLError("dns failure")))


def test_errors_never_carry_the_key():
    with pytest.raises(GoogleRefused) as failure:
        run(FakeGoogle(error=_http(403)))
    assert FAKE_KEY not in str(failure.value)
    assert FAKE_KEY not in failure.value.user_message


# ── Routes home (step 2) ─────────────────────────────────────────────────────

HOME_TRIP = {"location": "South Kensington", "destination": "N1 9GU"}


def test_no_destination_means_no_tfl_and_no_route_fields():
    tfl = FakeTfl()
    data = run(FakeGoogle([CLUB_3AM]), tfl=tfl).data
    assert tfl.urls == []
    for field in ("routes_home", "route_disruptions", "destination_used",
                  "latest_venue_home_at", "route_note"):
        assert field not in data


def test_the_route_home_leaves_at_closing_time():
    tfl = FakeTfl()
    data = run(FakeGoogle([CLUB_3AM]), HOME_TRIP, tfl=tfl).data
    query = parse_qs(urlsplit(tfl.urls[0]).query)
    assert query["date"] == ["20261121"] and query["time"] == ["0300"]
    assert query["timeIs"] == ["Departing"]
    assert data["destination_used"] == "90 York Wy, London N1 9GU"
    assert data["routes_home"] == [
        "Luna Kensington: leave at 3am, home 3:59am (50 min from 3:09am): "
        "walk 4 min > 14 bus > Victoria line > walk 15 min"]


def test_earliest_arrival_wins_over_shortest_trip():
    data = run(FakeGoogle([CLUB_3AM]), HOME_TRIP).data
    assert data["latest_venue_home_at"] == "2026-11-21T03:59:00+00:00"
    assert data["latest_venue_journey_min"] == 50
    assert data["latest_venue_route"] == "walk 4 min > 14 bus > Victoria line > walk 15 min"


def test_disruptions_are_only_for_lines_used_and_not_good_service():
    data = run(FakeGoogle([CLUB_3AM]), HOME_TRIP).data
    # N91 is only on the journey not chosen; the 14 has a good service.
    assert data["route_disruptions"] == [
        "Victoria: Severe Delays (Severe delays due to an earlier faulty train.)"]


def test_only_the_top_five_get_routes():
    six = [place(f"Pub {n}", every_day("09:00", "00:00", overnight=True)) for n in range(6)]
    tfl = FakeTfl()
    data = run(FakeGoogle(six), HOME_TRIP, tfl=tfl).data
    assert len(data["venues"]) == 6
    assert len(tfl.urls) == 5
    assert len(data["routes_home"]) == 5


def test_a_24_hour_venue_routes_from_now():
    allday = place("Glamis Bar", {"periods": [{"open": {"day": 0, "hour": 0, "minute": 0}}]},
                   kind="bar")
    tfl = FakeTfl()
    data = run(FakeGoogle([allday]), HOME_TRIP, tfl=tfl).data
    assert parse_qs(urlsplit(tfl.urls[0]).query)["time"] == ["2300"]
    assert data["routes_home"][0].startswith("Glamis Bar: leave now, home 3:59am")


def test_tfl_times_get_the_summer_offset():
    july = datetime(2026, 7, 17, 22, 0, tzinfo=timezone.utc)     # 23:00 BST
    summer = {"journeys": [tfl_journey("2026-07-18T03:05:00", "2026-07-18T03:50:00",
                                       [leg("bus", 45, "N19")])], "lines": []}
    data = run(FakeGoogle([dict(CLUB_3AM, utcOffsetMinutes=60)]), HOME_TRIP,
               now=july, tfl=FakeTfl(summer)).data
    assert data["latest_venue_home_at"] == "2026-07-18T03:50:00+01:00"


def test_the_tfl_request_goes_to_tfl_with_the_key_in_the_query():
    tfl = FakeTfl()
    run(FakeGoogle([CLUB_3AM]), HOME_TRIP, tfl=tfl)
    parts = urlsplit(tfl.urls[0])
    assert (parts.scheme, parts.netloc) == ("https", "api.tfl.gov.uk")
    assert parts.path.startswith("/Journey/JourneyResults/")
    assert parse_qs(parts.query)["app_key"] == [FAKE_TFL_KEY]


def test_without_a_tfl_key_it_still_asks_tfl(monkeypatch):
    monkeypatch.delenv("TFL_APP_KEY", raising=False)
    tfl = FakeTfl()
    data = run(FakeGoogle([CLUB_3AM]), HOME_TRIP, tfl=tfl).data
    assert "app_key" not in parse_qs(urlsplit(tfl.urls[0]).query)
    assert len(data["routes_home"]) == 1


def test_a_destination_google_cannot_find_still_returns_the_venues():
    data = run(FakeGoogle([CLUB_3AM], dest={}), HOME_TRIP).data
    assert data["venues"]
    assert data["route_note"] == "I could not find N1 9GU in the UK, so there are no routes home."
    assert "routes_home" not in data


def test_no_journeys_anywhere_says_tfl_is_london_only():
    data = run(FakeGoogle([CLUB_3AM]), HOME_TRIP, tfl=FakeTfl({"journeys": []})).data
    assert data["routes_home"] == ["Luna Kensington: no route found at that time"]
    assert "in and around London only" in data["route_note"]


@pytest.mark.parametrize("error, words", [
    (urllib.error.HTTPError("https://api.tfl.gov.uk/x", 404, "nf", {}, None), "no route found"),
    (urllib.error.HTTPError("https://api.tfl.gov.uk/x", 429, "busy", {}, None), "TfL is busy"),
    (urllib.error.HTTPError("https://api.tfl.gov.uk/x", 500, "err", {}, None), "not answering"),
    (TimeoutError("read timed out"), "did not answer in time"),
    (urllib.error.URLError("dns"), "could not be reached"),
])
def test_a_tfl_failure_never_sinks_the_answer(error, words):
    data = run(FakeGoogle([CLUB_3AM]), HOME_TRIP, tfl=FakeTfl(error)).data
    assert data["venues"]
    assert words in data["routes_home"][0]
    assert FAKE_TFL_KEY not in json.dumps(data)


def test_one_failed_route_does_not_spoil_the_others():
    def answer(query):
        return {"journeys": []} if query["time"] == ["0000"] else NIGHT_ROUTE
    data = run(FakeGoogle([PUB_MIDNIGHT, CLUB_3AM]), HOME_TRIP, tfl=FakeTfl(answer)).data
    assert data["routes_home"][0].startswith("Luna Kensington: leave at 3am")
    assert data["routes_home"][1] == "Zetland Arms: no route found at that time"
    assert "route_note" not in data


def test_the_narrative_mentions_getting_home():
    result = run(FakeGoogle([CLUB_3AM]), HOME_TRIP)
    assert "leaving at 3am, you'd reach 90 York Wy, London N1 9GU at 3:59am" in result.narrative


def test_leg_wording():
    assert routes._leg_words(leg("tube", 7, "Victoria")) == "Victoria line"
    assert routes._leg_words(leg("bus", 8, "N205")) == "N205 bus"
    assert routes._leg_words(leg("elizabeth-line", 9, "Elizabeth line")) == "Elizabeth line"
    assert routes._leg_words(leg("walking", 0)) == ""


def test_routes_go_to_places_that_close_before_24_hour_ones():
    closers = [place(f"Pub {n}", every_day("09:00", "00:00", overnight=True)) for n in range(5)]
    alldays = [place(f"Hotel Bar {n}", ALLDAY, kind="bar") for n in range(2)]
    tfl = FakeTfl()
    data = run(FakeGoogle(alldays + closers), HOME_TRIP, tfl=tfl).data
    assert len(tfl.urls) == 5
    assert all(r.startswith("Pub ") for r in data["routes_home"])
    assert data["venues"][-1].startswith("Hotel Bar ")


def test_24_hour_places_get_routes_when_fewer_than_five_close():
    alldays = [place(f"Hotel Bar {n}", ALLDAY, kind="bar") for n in range(4)]
    tfl = FakeTfl()
    data = run(FakeGoogle(alldays + [CLUB_3AM, PUB_MIDNIGHT]), HOME_TRIP, tfl=tfl).data
    assert [r.split(":")[0] for r in data["routes_home"]] == [
        "Luna Kensington", "Zetland Arms", "Hotel Bar 0", "Hotel Bar 1", "Hotel Bar 2"]
    assert "leave now" in data["routes_home"][2]


# ── open_until ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text, hour, minute", [
    ("2am", 2, 0), ("2 am", 2, 0), ("1:30am", 1, 30), ("1.30 a.m.", 1, 30),
    ("midnight", 0, 0), ("12am", 0, 0), ("12pm", 12, 0), ("11pm", 23, 0),
    ("02:00", 2, 0), ("23:30", 23, 30), ("0:30", 0, 30),
    ("2", 2, 0), ("11", 23, 0), ("12", 0, 0), ("3 o'clock", 3, 0),
])
def test_clock_times_people_type(text, hour, minute):
    parsed = venues.parse_clock(text)
    assert (parsed.hour, parsed.minute) == (hour, minute)


@pytest.mark.parametrize("text", ["late", "13am", "2:75", "25:00", "soon", "0am"])
def test_things_that_are_not_clock_times(text):
    assert venues.parse_clock(text) is None


@pytest.mark.parametrize("now, clock, expected", [
    (FRI_2300, "2am", "2026-11-21T02:00:00+00:00"),          # tonight, past midnight
    (FRI_2300, "midnight", "2026-11-21T00:00:00+00:00"),
    (FRI_2300, "11pm", "2026-11-20T23:00:00+00:00"),         # just now: not tomorrow
    (FRI_2300, "10pm", "2026-11-20T22:00:00+00:00"),
    (SAT_0130, "3am", "2026-11-21T03:00:00+00:00"),
    (SAT_0130, "midnight", "2026-11-21T00:00:00+00:00"),     # the one just gone
    (datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc), "midnight",
     "2026-11-21T00:00:00+00:00"),                           # tonight's, said in the morning
])
def test_open_until_means_tonight(now, clock, expected):
    assert venues.tonight_at(venues.parse_clock(clock), now).isoformat() == expected


def test_open_until_keeps_only_places_open_that_late():
    allday = place("Glamis Bar", ALLDAY, kind="bar")
    data = run(FakeGoogle([PUB_2300, PUB_MIDNIGHT, CLUB_3AM, allday]),
               {"location": "South Kensington", "open_until": "2am"}).data
    names = [line.split(" (")[0] for line in data["venues"]]
    assert names == ["Luna Kensington", "Glamis Bar"]
    assert data["open_until_used"] == "2am Saturday"
    assert data["closing_too_early"] == 1           # Zetland Arms; the 11pm pub is shut
    assert data["open_count"] == 3


def test_open_until_counts_a_close_exactly_at_that_time():
    data = run(FakeGoogle([PUB_MIDNIGHT]),
               {"location": "South Kensington", "open_until": "midnight"}).data
    assert data["venues"][0].startswith("Zetland Arms")
    assert data["closing_too_early"] == 0


def test_open_until_in_summer_is_london_time():
    summer = datetime(2026, 6, 19, 22, 0, tzinfo=timezone.utc)     # 23:00 BST
    data = run(FakeGoogle([place("Summer Bar", every_day("12:00", "02:00", overnight=True),
                                 utcOffsetMinutes=60, kind="bar")]),
               {"location": "South Kensington", "open_until": "2am"}, now=summer).data
    assert data["open_until_used"] == "2am Saturday"
    assert data["venues"][0].startswith("Summer Bar")


def test_nothing_open_that_late_names_the_latest():
    result = run(FakeGoogle([PUB_MIDNIGHT, CLUB_3AM]),
                 {"location": "South Kensington", "open_until": "4am"})
    assert result.data["venues"] == []
    assert result.data["closing_too_early"] == 2
    assert "latest_venue" not in result.data
    assert "stays open till 4am" in result.narrative
    assert "The latest is Luna Kensington, open till 3am." in result.narrative


def test_open_until_routes_only_the_late_ones():
    tfl = FakeTfl()
    data = run(FakeGoogle([PUB_MIDNIGHT, CLUB_3AM]),
               dict(HOME_TRIP, open_until="1am"), tfl=tfl).data
    assert len(tfl.urls) == 1
    assert data["routes_home"][0].startswith("Luna Kensington")


def test_an_unreadable_time_says_what_to_type():
    with pytest.raises(BadTime) as caught:
        run(FakeGoogle([CLUB_3AM]), {"location": "Soho", "open_until": "whenever"},
            explicit=True)
    assert caught.value.user_message == (
        'I could not read "whenever" as a closing time; '
        "try something like 2am, 1:30am or midnight.")


def test_no_open_until_means_no_filter_fields():
    data = run(FakeGoogle([CLUB_3AM])).data
    assert "open_until_used" not in data and "closing_too_early" not in data
