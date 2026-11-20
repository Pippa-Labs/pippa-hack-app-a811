"""Tests for Running Tab, run through LocalRunner so the manifest is enforced.

The network is blocked (conftest.py). Google is replaced by stubbing
`App._post`, the one method that does HTTP. The payloads below have the same
shape as real Places API (New) responses captured on 20 Nov 2026.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

import pytest
from pippa_agentapp_sdk import LocalRunner, RateLimitedError, ServiceUnavailableError

import venues
from app import (
    NEARBY_SEARCH_URL,
    TEXT_SEARCH_URL,
    App,
    GoogleRefused,
    KeyMissing,
    NoLocation,
    PlaceNotFound,
)

FAKE_KEY = "test-key-not-real-000000000000"  # pragma: allowlist secret

# Friday 20 November 2026, 23:00 in London (GMT, so UTC+0).
FRI_2300 = datetime(2026, 11, 20, 23, 0, tzinfo=timezone.utc)
SAT_0130 = datetime(2026, 11, 21, 1, 30, tzinfo=timezone.utc)
SUN_0100 = datetime(2026, 11, 22, 1, 0, tzinfo=timezone.utc)

CENTRE = {"latitude": 51.4941, "longitude": -0.1738}


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

    def __init__(self, nearby=None, text=None, error=None):
        self.nearby = {"places": nearby or []}
        self.text = text if text is not None else {
            "places": [{"displayName": {"text": "South Kensington"},
                        "formattedAddress": "South Kensington, London",
                        "location": CENTRE}]}
        self.error = error
        self.calls = []

    def __call__(self, url, body, field_mask, key, timeout=6.0):
        self.calls.append({"url": url, "body": body, "key": key})
        if self.error is not None:
            raise self.error
        payload = self.text if url == TEXT_SEARCH_URL else self.nearby
        return json.dumps(payload).encode("utf-8")


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", FAKE_KEY)


def run(google, inputs=None, now=FRI_2300, explicit=False):
    app = App()
    app._post = google
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


def test_open_24_hours_ranks_first():
    allday = place("Billionaire Lounge", {"periods": [{"open": {"day": 0, "hour": 0, "minute": 0}}]},
                   kind="lounge_bar")
    data = run(FakeGoogle([CLUB_3AM, allday])).data
    assert data["venues"][0].startswith("Billionaire Lounge")
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
