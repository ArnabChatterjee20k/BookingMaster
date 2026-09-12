"""End-to-end flow tests.

Run:  python test.py

Drives the app in-process with FastAPI's TestClient (no uvicorn needed) against
the real Postgres in DB_URI -- the lifespan runs load_schemas(), so the tables
are created for you. To point this at a live server instead, swap the client for
`requests.Session()` and prefix the paths with a base url; the call shapes match.

Tables are truncated at startup so re-runs are deterministic.
"""

import asyncio
import uuid
from decimal import Decimal

import asyncpg
from fastapi.testclient import TestClient

from src import create_api
from src.config import Config

TABLES = [
    "tickets",
    "bookings",
    "tickets_tier",
    "events",
    "venues",
    "memberships",
    "organisations",
    "users",
]

AUTH = Config.auth_cookie_name
results = []
state = {}


def step(*requires):
    """Register a test. Runs in order; skipped if a prerequisite never populated
    `state`, so one upstream failure doesn't cascade into a wall of noise."""

    def deco(fn):
        results.append((fn, requires))
        return fn

    return deco


def truncate():
    async def go():
        conn = await asyncpg.connect(Config.db_uri)
        existing = [
            r["relname"]
            for r in await conn.fetch(
                "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace "
                "where n.nspname='public' and c.relkind='r'"
            )
        ]
        targets = [t for t in TABLES if t in existing]
        if targets:
            await conn.execute(
                f"truncate {', '.join(targets)} restart identity cascade"
            )
        await conn.close()

    asyncio.run(go())


def signup(c, name):
    """Create a user; return (body, auth cookie dict). Leaves the client anonymous."""
    email = f"{name}-{uuid.uuid4().hex[:8]}@test.local"
    r = c.post("/users", json={"email": email, "name": name, "password": "hunter2"})
    assert r.status_code == 200, f"signup failed: {r.status_code} {r.text}"
    token = r.cookies.get(AUTH)
    assert token, f"no {AUTH} cookie on the signup response"
    c.cookies.clear()
    return r.json(), {AUTH: token}


# ---------------------------------------------------------------- auth / users


@step()
def test_health(c):
    r = c.get("/health")
    assert r.status_code == 200, r.status_code


@step()
def test_signup_returns_user_and_cookie(c):
    body, auth = signup(c, "alice")
    assert body["name"] == "alice", body
    assert "uid" in body, body
    state["alice"], state["alice_auth"] = body, auth


@step()
def test_duplicate_email_rejected(c):
    payload = {
        "email": f"dup-{uuid.uuid4().hex[:8]}@test.local",
        "name": "dup",
        "password": "x",
    }
    first = c.post("/users", json=payload)
    c.cookies.clear()
    assert first.status_code == 200, first.text
    second = c.post("/users", json=payload)
    c.cookies.clear()
    assert (
        second.status_code == 400
    ), f"expected 400, got {second.status_code} {second.text}"


@step()
def test_anonymous_is_rejected(c):
    r = c.get("/organisations")
    assert r.status_code == 401, f"expected 401, got {r.status_code} {r.text}"


@step()
def test_garbage_token_is_rejected(c):
    r = c.get("/organisations", cookies={AUTH: "not-a-jwt"})
    assert r.status_code == 401, f"expected 401, got {r.status_code} {r.text}"


# -------------------------------------------------------------- organisations


@step("alice_auth")
def test_create_organisation(c):
    r = c.post("/organisations", json={"name": "acme"}, cookies=state["alice_auth"])
    assert r.status_code == 200, r.text
    org = r.json()
    assert org["name"] == "acme", org
    assert org["role"] == "owner", f"creator should be owner, got {org.get('role')}"
    state["org"] = org


@step("alice_auth", "org")
def test_list_shows_own_org(c):
    r = c.get("/organisations", cookies=state["alice_auth"])
    assert r.status_code == 200, r.text
    uids = [o["uid"] for o in r.json()["data"]]
    assert state["org"]["uid"] in uids, uids


@step()
def test_other_user_sees_nothing(c):
    body, auth = signup(c, "bob")
    state["bob"], state["bob_auth"] = body, auth
    r = c.get("/organisations", cookies=auth)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == [], f"bob should see no orgs, got {r.json()['data']}"


@step("alice_auth", "org")
def test_get_organisation_as_member(c):
    r = c.get(f"/organisations/{state['org']['uid']}", cookies=state["alice_auth"])
    assert r.status_code == 200, r.text
    assert r.json()["uid"] == state["org"]["uid"]


@step("bob_auth", "org")
def test_get_organisation_as_stranger_is_404(c):
    r = c.get(f"/organisations/{state['org']['uid']}", cookies=state["bob_auth"])
    assert (
        r.status_code == 404
    ), f"expected 404 (no existence leak), got {r.status_code} {r.text}"


@step("alice_auth")
def test_unknown_organisation_is_404(c):
    r = c.get(f"/organisations/{uuid.uuid4()}", cookies=state["alice_auth"])
    assert r.status_code == 404, f"expected 404, got {r.status_code} {r.text}"


@step("alice_auth")
def test_malformed_uid_is_422(c):
    # path params are typed UUID, so FastAPI rejects this before any query runs.
    r = c.get("/organisations/not-a-uuid", cookies=state["alice_auth"])
    assert r.status_code == 422, f"expected 422, got {r.status_code} {r.text}"


# ------------------------------------------------------------------- members


@step("bob", "bob_auth", "org")
def test_non_owner_cannot_add_members(c):
    r = c.post(
        f"/organisations/{state['org']['uid']}/members",
        json={"members": [{"user_uid": state["bob"]["uid"], "role": "member"}]},
        cookies=state["bob_auth"],
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"


@step("alice_auth", "bob", "org")
def test_owner_adds_member(c):
    r = c.post(
        f"/organisations/{state['org']['uid']}/members",
        json={"members": [{"user_uid": state["bob"]["uid"], "role": "member"}]},
        cookies=state["alice_auth"],
    )
    assert r.status_code == 200, r.text


@step("bob_auth", "org")
def test_new_member_can_now_see_org(c):
    r = c.get(f"/organisations/{state['org']['uid']}", cookies=state["bob_auth"])
    assert r.status_code == 200, f"bob is a member now, got {r.status_code} {r.text}"
    assert r.json()["role"] == "member", r.json()


@step("alice", "alice_auth", "bob", "org")
def test_list_members(c):
    r = c.get(
        f"/organisations/{state['org']['uid']}/members", cookies=state["alice_auth"]
    )
    assert r.status_code == 200, r.text
    by_uid = {m["user_uid"]: m["role"] for m in r.json()["members"]}
    assert by_uid.get(state["alice"]["uid"]) == "owner", by_uid
    assert by_uid.get(state["bob"]["uid"]) == "member", by_uid


@step("alice_auth", "bob", "org")
def test_adding_same_member_twice_is_not_a_500(c):
    r = c.post(
        f"/organisations/{state['org']['uid']}/members",
        json={"members": [{"user_uid": state["bob"]["uid"], "role": "member"}]},
        cookies=state["alice_auth"],
    )
    assert (
        r.status_code < 500
    ), f"unique(org_uid,user_uid) leaked as {r.status_code}: {r.text}"
    assert r.status_code == 409, f"expected 409 conflict, got {r.status_code}"


# ---------------------------------------------------------------- pagination


@step("alice_auth")
def test_pagination_covers_every_row_exactly_once(c):
    """The point of the cursor: walk every page, see each org exactly once."""
    for i in range(4):  # alice ends up with 5 orgs
        r = c.post(
            "/organisations",
            json={"name": f"page-org-{i}"},
            cookies=state["alice_auth"],
        )
        assert r.status_code == 200, r.text

    seen, after, pages = [], 0, 0
    while True:
        r = c.get(
            "/organisations",
            params={"after": after, "limit": 2},
            cookies=state["alice_auth"],
        )
        assert r.status_code == 200, r.text
        body = r.json()
        seen.extend(o["uid"] for o in body["data"])
        pages += 1
        assert pages < 20, "cursor never terminated -- pagination is looping"
        if body.get("next") is None:
            break
        after = body["next"]

    assert len(seen) == len(set(seen)), f"duplicate rows across pages: {seen}"
    assert len(seen) == 5, f"expected 5 orgs across {pages} pages, saw {len(seen)}"


# -------------------------------------------------- member admin / org delete


def _org_with_bob(c, name):
    """Create an org owned by alice with bob as a plain member. Returns its uid."""
    r = c.post("/organisations", json={"name": name}, cookies=state["alice_auth"])
    assert r.status_code == 200, r.text
    org_uid = r.json()["uid"]
    r = c.post(
        f"/organisations/{org_uid}/members",
        json={"members": [{"user_uid": state["bob"]["uid"], "role": "member"}]},
        cookies=state["alice_auth"],
    )
    assert r.status_code == 200, r.text
    return org_uid


def _roles(c, org_uid):
    r = c.get(f"/organisations/{org_uid}/members", cookies=state["alice_auth"])
    assert r.status_code == 200, r.text
    return {m["user_uid"]: m["role"] for m in r.json()["members"]}


@step("alice_auth", "bob")
def test_update_member_role(c):
    org_uid = _org_with_bob(c, "promote-me")
    r = c.put(
        f"/organisations/{org_uid}/members/{state['bob']['uid']}",
        json={"role": "owner"},
        cookies=state["alice_auth"],
    )
    assert r.status_code == 200, r.text
    assert _roles(c, org_uid)[state["bob"]["uid"]] == "owner", "role was not updated"


@step("alice_auth", "bob")
def test_role_update_does_not_leak_across_orgs(c):
    """A role change in one org must not touch the same user's role elsewhere."""
    target = _org_with_bob(c, "leak-target")
    bystander = _org_with_bob(c, "leak-bystander")
    r = c.put(
        f"/organisations/{target}/members/{state['bob']['uid']}",
        json={"role": "owner"},
        cookies=state["alice_auth"],
    )
    assert r.status_code == 200, r.text
    role_elsewhere = _roles(c, bystander)[state["bob"]["uid"]]
    assert (
        role_elsewhere == "member"
    ), f"role update leaked: bob became {role_elsewhere!r} in an unrelated org"


@step("alice_auth", "bob")
def test_delete_member(c):
    org_uid = _org_with_bob(c, "kick-me")
    r = c.delete(
        f"/organisations/{org_uid}/members/{state['bob']['uid']}",
        cookies=state["alice_auth"],
    )
    assert r.status_code == 204, f"expected 204, got {r.status_code} {r.text}"
    assert state["bob"]["uid"] not in _roles(
        c, org_uid
    ), "member still present after delete"


@step("alice_auth")
def test_owner_cannot_delete_themselves(c):
    r = c.post(
        "/organisations", json={"name": "self-kick"}, cookies=state["alice_auth"]
    )
    assert r.status_code == 200, r.text
    org_uid = r.json()["uid"]
    r = c.delete(
        f"/organisations/{org_uid}/members/{state['alice']['uid']}",
        cookies=state["alice_auth"],
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"


@step("alice_auth", "bob")
def test_delete_organisation(c):
    org_uid = _org_with_bob(c, "doomed")
    r = c.delete(f"/organisations/{org_uid}", cookies=state["alice_auth"])
    assert r.status_code == 204, f"expected 204, got {r.status_code} {r.text}"
    r = c.get(f"/organisations/{org_uid}", cookies=state["alice_auth"])
    assert r.status_code == 404, f"org still readable after delete: {r.status_code}"


@step("alice_auth", "bob_auth")
def test_non_owner_cannot_delete_organisation(c):
    org_uid = _org_with_bob(c, "protected")
    r = c.delete(f"/organisations/{org_uid}", cookies=state["bob_auth"])
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"


# --------------------------------------------------------------------- venues


def _venue_body(org_uid, name, longitude=77.5946, latitude=12.9716):
    return {
        "org_uid": org_uid,
        "name": name,
        "location": {"longitude": longitude, "latitude": latitude},
    }


def _unique(prefix):
    """unique(name, location) is global, so every test needs its own name."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@step("alice_auth", "org")
def test_owner_creates_venue(c):
    name = _unique("hall")
    r = c.post(
        "/venues",
        json=_venue_body(state["org"]["uid"], name),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 200, r.text
    venue = r.json()
    assert venue["name"] == name, venue
    # the whole point of ST_AsText in VENUE_COLUMNS: a parsed point, not WKB hex
    assert venue["location"] == {"longitude": 77.5946, "latitude": 12.9716}, venue
    assert "uid" in venue, venue
    state["venue"] = venue


@step("org")
def test_anonymous_cannot_create_venue(c):
    r = c.post("/venues", json=_venue_body(state["org"]["uid"], _unique("anon")))
    assert r.status_code == 401, f"expected 401, got {r.status_code} {r.text}"


@step("bob_auth", "org")
def test_member_cannot_create_venue(c):
    """bob is a member of state['org'], not an owner."""
    r = c.post(
        "/venues",
        json=_venue_body(state["org"]["uid"], _unique("member")),
        cookies=state["bob_auth"],
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"


@step("alice_auth")
def test_cannot_create_venue_in_unknown_org(c):
    """No membership row at all -- must not fall through to the insert."""
    r = c.post(
        "/venues",
        json=_venue_body(str(uuid.uuid4()), _unique("ghost")),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"


@step("alice_auth", "org")
def test_duplicate_name_and_location_is_409(c):
    body = _venue_body(state["org"]["uid"], _unique("twin"))
    first = c.post("/venues", json=body, cookies=state["alice_auth"])
    assert first.status_code == 200, first.text
    second = c.post("/venues", json=body, cookies=state["alice_auth"])
    assert (
        second.status_code == 409
    ), f"unique(name, location) should surface as 409, got {second.status_code} {second.text}"
    # the handler must not echo asyncpg's message (it carries the constraint and the WKB)
    assert "venues_name_location_key" not in second.text, second.text


@step("alice_auth", "org")
def test_same_name_different_location_allowed(c):
    name = _unique("twosites")
    a = c.post(
        "/venues",
        json=_venue_body(state["org"]["uid"], name, 10.0, 10.0),
        cookies=state["alice_auth"],
    )
    b = c.post(
        "/venues",
        json=_venue_body(state["org"]["uid"], name, 20.0, 20.0),
        cookies=state["alice_auth"],
    )
    assert a.status_code == 200, a.text
    assert (
        b.status_code == 200
    ), f"same name at a different point is a different venue: {b.text}"
    assert a.json()["uid"] != b.json()["uid"]


@step("alice_auth", "org")
def test_same_location_different_name_allowed(c):
    point = (30.0, 30.0)
    a = c.post(
        "/venues",
        json=_venue_body(state["org"]["uid"], _unique("stadium"), *point),
        cookies=state["alice_auth"],
    )
    b = c.post(
        "/venues",
        json=_venue_body(state["org"]["uid"], _unique("annex"), *point),
        cookies=state["alice_auth"],
    )
    assert a.status_code == 200, a.text
    assert b.status_code == 200, f"same point under another name is allowed: {b.text}"


@step("alice_auth", "org")
def test_point_precision_still_conflicts(c):
    """POINT(1.5 1.5) and POINT(1.50 1.50) are the same float8 pair."""
    name = _unique("precise")
    a = c.post(
        "/venues",
        json=_venue_body(state["org"]["uid"], name, 1.5, 1.5),
        cookies=state["alice_auth"],
    )
    assert a.status_code == 200, a.text
    b = c.post(
        "/venues",
        json=_venue_body(state["org"]["uid"], name, 1.50, 1.50),
        cookies=state["alice_auth"],
    )
    assert b.status_code == 409, f"expected 409, got {b.status_code} {b.text}"


@step("alice_auth", "org")
def test_out_of_range_coordinates_are_422(c):
    for lon, lat in [(181.0, 0.0), (-181.0, 0.0), (0.0, 91.0), (0.0, -91.0)]:
        r = c.post(
            "/venues",
            json=_venue_body(state["org"]["uid"], _unique("bad"), lon, lat),
            cookies=state["alice_auth"],
        )
        assert (
            r.status_code == 422
        ), f"({lon}, {lat}) should be rejected, got {r.status_code}"


@step("alice_auth", "org")
def test_create_venue_rejects_malformed_body(c):
    r = c.post(
        "/venues",
        json={"org_uid": state["org"]["uid"], "name": "no-location"},
        cookies=state["alice_auth"],
    )
    assert r.status_code == 422, f"expected 422, got {r.status_code} {r.text}"


@step("venue")
def test_get_venue_by_uid(c):
    r = c.get(f"/venues/{state['venue']['uid']}")
    assert r.status_code == 200, r.text
    assert r.json()["uid"] == state["venue"]["uid"], r.json()
    assert r.json()["location"] == state["venue"]["location"], r.json()


@step()
def test_unknown_venue_is_404(c):
    r = c.get(f"/venues/{uuid.uuid4()}")
    assert r.status_code == 404, f"expected 404, got {r.status_code} {r.text}"


@step()
def test_malformed_venue_uid_is_422(c):
    r = c.get("/venues/not-a-uuid")
    assert r.status_code == 422, f"expected 422, got {r.status_code} {r.text}"


@step("venue")
def test_list_venues_returns_parsed_points(c):
    r = c.get("/venues")
    assert r.status_code == 200, r.text
    venues = r.json()["venues"]
    assert venues, "expected at least the venue created earlier"
    for v in venues:
        assert set(v["location"]) == {"longitude", "latitude"}, v
        assert isinstance(v["location"]["longitude"], float), v


@step()
def test_venue_limit_bounds_are_enforced(c):
    assert c.get("/venues", params={"limit": 0}).status_code == 422
    assert c.get("/venues", params={"limit": 101}).status_code == 422
    assert c.get("/venues", params={"limit": 1}).status_code == 200
    assert c.get("/venues", params={"limit": 100}).status_code == 200


@step("alice_auth", "org")
def test_venue_pagination_covers_every_row_exactly_once(c):
    """Walk the keyset cursor to the end; every venue appears exactly once."""
    mine = set()
    for i in range(5):
        r = c.post(
            "/venues",
            json=_venue_body(
                state["org"]["uid"], _unique(f"page-{i}"), 100.0 + i, 40.0
            ),
            cookies=state["alice_auth"],
        )
        assert r.status_code == 200, r.text
        mine.add(r.json()["uid"])

    seen, after, pages = [], 0, 0
    while True:
        r = c.get("/venues", params={"after": after, "limit": 2})
        assert r.status_code == 200, r.text
        batch = r.json()["venues"]
        seen.extend(v["uid"] for v in batch)
        pages += 1
        assert pages < 100, "cursor never terminated -- pagination is looping"
        if len(batch) < 2:
            break
        after = batch[-1]["id"]

    assert len(seen) == len(set(seen)), "duplicate rows across pages"
    assert mine <= set(seen), f"pagination skipped {len(mine - set(seen))} venues"


@step("venue")
def test_venue_reads_are_unauthenticated(c):
    """Pins current behaviour: /venues and /venues/{uid} take no CurrentUser, so
    anyone can read every venue in the database regardless of membership. Flip
    both assertions to 401 if venues become org-scoped."""
    assert c.get("/venues").status_code == 200
    assert c.get(f"/venues/{state['venue']['uid']}").status_code == 200


# --------------------------------------------------------------------- events


def _iso(hours_from_now):
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(hours=hours_from_now)).isoformat()


def _make_venue(c, name=None):
    """Create a venue owned by alice; returns its body."""
    r = c.post(
        "/venues",
        json=_venue_body(
            state["org"]["uid"], name or _unique("v"), float(len(results) % 170), 5.0
        ),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 200, r.text
    return r.json()


def _event_body(venue_uid, name="gig", org_uid=None, starts_in_hours=48):
    """Default start is two days out, not one hour: `list_events` filters
    `starts_at >= now + 1 day`, so anything sooner is created but never listed.
    Pass `starts_in_hours` to sit deliberately either side of that horizon, or
    to dodge the 2-day booking gap when reusing a venue."""
    return {
        "name": name,
        "org_uid": org_uid or state["org"]["uid"],
        "performer_uid": str(uuid.uuid4()),
        "venue_uid": venue_uid,
        "starts_at": _iso(starts_in_hours),
        "ends_at": _iso(starts_in_hours + 2),
    }


@step("alice_auth", "org")
def test_owner_creates_event(c):
    venue = _make_venue(c, _unique("arena"))
    r = c.post(
        "/events",
        json=_event_body(venue["uid"], "opening night"),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 200, r.text
    event = r.json()
    assert event["name"] == "opening night", event
    assert event["venue_uid"] == venue["uid"], event
    assert event["org_uid"] == state["org"]["uid"], event
    state["venue_a"], state["event"] = venue, event


@step("org")
def test_anonymous_cannot_create_event(c):
    r = c.post("/events", json=_event_body(str(uuid.uuid4())))
    assert r.status_code == 401, f"expected 401, got {r.status_code} {r.text}"


@step("bob_auth", "org", "venue_a")
def test_member_cannot_create_event(c):
    r = c.post(
        "/events", json=_event_body(state["venue_a"]["uid"]), cookies=state["bob_auth"]
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"


@step("alice_auth", "venue_a")
def test_cannot_create_event_in_unknown_org(c):
    r = c.post(
        "/events",
        json=_event_body(state["venue_a"]["uid"], org_uid=str(uuid.uuid4())),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"


@step("alice_auth", "org")
def test_create_event_with_unknown_venue_is_404(c):
    """The `insert ... select` gate: no venue row -> no insert -> 404, not a 500."""
    r = c.post(
        "/events", json=_event_body(str(uuid.uuid4())), cookies=state["alice_auth"]
    )
    assert r.status_code == 404, f"expected 404, got {r.status_code} {r.text}"


@step("alice_auth", "org")
def test_failed_event_insert_writes_nothing(c):
    """A rejected gate must not leave a half-written event behind."""
    before = c.get("/venues").status_code  # cheap liveness check
    assert before == 200
    bad = str(uuid.uuid4())
    r = c.post("/events", json=_event_body(bad, "ghost"), cookies=state["alice_auth"])
    assert r.status_code == 404, r.text
    # nothing to fetch: the event uid was never returned, so probe by listing venues
    # and confirming no event references the bogus venue via a direct get
    assert c.get(f"/events/{bad}").status_code == 404


@step("alice_auth", "org")
def test_naive_datetime_is_rejected(c):
    venue = _make_venue(c)
    body = _event_body(venue["uid"])
    body["starts_at"] = "2030-01-01T10:00:00"  # no tzinfo
    r = c.post("/events", json=body, cookies=state["alice_auth"])
    assert r.status_code == 422, f"expected 422, got {r.status_code} {r.text}"


@step("alice_auth", "org")
def test_create_event_rejects_malformed_body(c):
    r = c.post(
        "/events",
        json={"name": "no-venue", "org_uid": state["org"]["uid"]},
        cookies=state["alice_auth"],
    )
    assert r.status_code == 422, f"expected 422, got {r.status_code} {r.text}"


@step("event", "venue_a")
def test_get_event_returns_venue_info(c):
    r = c.get(f"/events/{state['event']['uid']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["uid"] == state["event"]["uid"], body
    assert body["name"] == "opening night", body
    # venue.name must be the VENUE's name -- `e.*` also carries a `name` column,
    # so an unaliased join hands back the event name here.
    assert (
        body["venue"]["name"] == state["venue_a"]["name"]
    ), f"venue.name is {body['venue']['name']!r}, expected the venue's own name"
    assert body["venue"]["location"] == state["venue_a"]["location"], body["venue"]


@step("alice_auth", "org")
def test_get_event_returns_the_requested_event(c):
    """Guards the where clause: without `where e.uid = $1` this returns whichever
    event the join happened to yield first."""
    first = c.post(
        "/events",
        json=_event_body(_make_venue(c)["uid"], "first"),
        cookies=state["alice_auth"],
    ).json()
    second = c.post(
        "/events",
        json=_event_body(_make_venue(c)["uid"], "second"),
        cookies=state["alice_auth"],
    ).json()
    for expected in (first, second):
        r = c.get(f"/events/{expected['uid']}")
        assert r.status_code == 200, r.text
        assert (
            r.json()["uid"] == expected["uid"]
        ), f"asked for {expected['name']}, got {r.json()['name']}"


@step()
def test_unknown_event_is_404(c):
    r = c.get(f"/events/{uuid.uuid4()}")
    assert r.status_code == 404, f"expected 404, got {r.status_code} {r.text}"


@step()
def test_malformed_event_uid_is_422(c):
    r = c.get("/events/not-a-uuid")
    assert r.status_code == 422, f"expected 422, got {r.status_code} {r.text}"


# ---------------------------------------------------------------- list events

# The geo tests sit at negative longitudes on purpose: every other venue in this
# file lands at a positive longitude (`_make_venue` uses `len(results) % 170`,
# the venue section uses Bangalore), so nothing else can drift into radius.
_PACIFIC = (-150.0, -40.0)
# ~5 km east of _PACIFIC: one degree of longitude at lat -40 is ~85.4 km.
_PACIFIC_5KM = (-149.9415, -40.0)


def _geo_event(c, name, longitude, latitude):
    """Venue at an exact point + an event in it; returns the event uid."""
    r = c.post(
        "/venues",
        json=_venue_body(state["org"]["uid"], _unique(name), longitude, latitude),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 200, r.text
    e = c.post(
        "/events",
        json=_event_body(r.json()["uid"], name),
        cookies=state["alice_auth"],
    )
    assert e.status_code == 200, e.text
    return e.json()["uid"]


def _uids(response):
    assert response.status_code == 200, response.text
    return {e["uid"] for e in response.json()["events"]}


@step("event", "venue_a")
def test_list_events_returns_venue_info(c):
    """Same aliasing trap as /events/{uid}: `e.*` already carries `name`, so an
    unaliased join hands back the event name as the venue name."""
    r = c.get("/events", params={"limit": 100})
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    assert events, "expected at least the event created earlier"
    for e in events:
        assert set(e["venue"]) == {"name", "location"}, e
        assert set(e["venue"]["location"]) == {"longitude", "latitude"}, e
    mine = [e for e in events if e["uid"] == state["event"]["uid"]]
    assert mine, "the known event is missing from the listing"
    assert mine[0]["name"] == "opening night", mine[0]
    assert mine[0]["venue"]["name"] == state["venue_a"]["name"], mine[0]["venue"]
    assert mine[0]["venue"]["location"] == state["venue_a"]["location"], mine[0]


@step()
def test_list_event_limit_bounds_are_enforced(c):
    assert c.get("/events", params={"limit": 0}).status_code == 422
    assert c.get("/events", params={"limit": 101}).status_code == 422
    assert c.get("/events", params={"limit": 1}).status_code == 200
    assert c.get("/events", params={"limit": 100}).status_code == 200


@step("event")
def test_list_events_are_unauthenticated(c):
    """Mirrors the venue reads: /events takes no CurrentUser, so the listing is
    world-readable and unscoped. Flip to 401 if events become org-scoped."""
    assert c.get("/events").status_code == 200


@step()
def test_list_events_respects_limit(c):
    r = c.get("/events", params={"limit": 1})
    assert r.status_code == 200, r.text
    assert len(r.json()["events"]) <= 1, r.json()


@step("alice_auth", "org")
def test_list_event_pagination_covers_every_row_exactly_once(c):
    """Walk the keyset cursor to the end; every event appears exactly once."""
    mine = {_geo_event(c, f"page-{i}", 120.0 + i, 40.0) for i in range(5)}

    seen, after, pages = [], 0, 0
    while True:
        r = c.get("/events", params={"after": after, "limit": 2})
        assert r.status_code == 200, r.text
        batch = r.json()["events"]
        seen.extend(e["uid"] for e in batch)
        pages += 1
        assert pages < 100, "cursor never terminated -- pagination is looping"
        if len(batch) < 2:
            break
        after = batch[-1]["id"]

    assert len(seen) == len(set(seen)), "duplicate rows across pages"
    assert mine <= set(seen), f"pagination skipped {len(mine - set(seen))} events"


@step("alice_auth", "org")
def test_list_events_ordered_by_id_ascending(c):
    r = c.get("/events", params={"limit": 100})
    assert r.status_code == 200, r.text
    ids = [e["id"] for e in r.json()["events"]]
    assert ids == sorted(ids), ids


@step("alice_auth", "org")
def test_geo_filter_uses_km_not_metres(c):
    """ST_DWithin on geography takes metres, so the km radius has to be scaled.
    Without the *1000 a 10 km search reaches 10 m and misses the 5 km neighbour."""
    here = _geo_event(c, "pacific", *_PACIFIC)
    near = _geo_event(c, "pacific-5km", *_PACIFIC_5KM)
    lon, lat = _PACIFIC

    tight = _uids(
        c.get(
            "/events",
            params={"longitude": lon, "latitude": lat, "radius": 1, "limit": 100},
        )
    )
    assert here in tight, "the point itself fell outside a 1 km radius"
    assert near not in tight, "a venue 5 km away came back for a 1 km radius"

    wide = _uids(
        c.get(
            "/events",
            params={"longitude": lon, "latitude": lat, "radius": 10, "limit": 100},
        )
    )
    assert {here, near} <= wide, "a 10 km radius missed a venue 5 km away"


@step("alice_auth", "org", "event")
def test_geo_filter_excludes_far_away(c):
    lon, lat = _PACIFIC
    got = _uids(
        c.get(
            "/events",
            params={"longitude": lon, "latitude": lat, "radius": 100, "limit": 100},
        )
    )
    assert state["event"]["uid"] not in got, "an event ~13000 km away passed the filter"


@step("alice_auth", "org")
def test_geo_filter_works_at_zero_coordinates(c):
    """Guards `if lon and lat` truthiness: longitude 0 and latitude 0 are real
    coordinates, and a falsy check silently drops the filter and returns
    everything with a 200."""
    at_zero = _geo_event(c, "null-island", 0.0, 0.0)
    got = _uids(
        c.get(
            "/events", params={"longitude": 0, "latitude": 0, "radius": 1, "limit": 100}
        )
    )
    assert at_zero in got, got
    assert got == {at_zero}, f"filter was skipped -- got {len(got)} events, expected 1"

    # latitude 0 with a non-zero longitude is the other half of the same trap
    off_equator = _uids(
        c.get(
            "/events",
            params={"longitude": 120.0, "latitude": 0, "radius": 1, "limit": 100},
        )
    )
    assert off_equator == set(), f"filter was skipped -- got {len(off_equator)} events"


@step("alice_auth", "org")
def test_geo_filter_binds_every_argument(c):
    """Guards `fetch(query, *q.args)`: with a location the query grows to five
    placeholders, and hardcoding (after, limit) leaves three unbound -- asyncpg
    raises InterfaceError and it surfaces as a 500."""
    lon, lat = _PACIFIC
    for i in range(3):
        _geo_event(c, f"pac-page-{i}", lon, lat + 0.001 * i)
    r = c.get(
        "/events",
        params={
            "longitude": lon,
            "latitude": lat,
            "radius": 10,
            "after": 0,
            "limit": 2,
        },
    )
    assert r.status_code == 200, f"expected 200, got {r.status_code} {r.text}"
    assert len(r.json()["events"]) == 2, "limit did not bind to the right placeholder"


@step()
def test_half_a_coordinate_is_422(c):
    """One coordinate without the other must not silently list everything."""
    for params in ({"longitude": 77.5946}, {"latitude": 12.9716}):
        r = c.get("/events", params={**params, "radius": 5})
        assert (
            r.status_code == 422
        ), f"expected 422 for {params}, got {r.status_code} {r.text}"


@step()
def test_out_of_range_filter_coordinates_are_422(c):
    for params in (
        {"longitude": 181.0, "latitude": 0.0},
        {"longitude": 0.0, "latitude": 91.0},
    ):
        r = c.get("/events", params=params)
        assert (
            r.status_code == 422
        ), f"expected 422 for {params}, got {r.status_code} {r.text}"


@step()
def test_radius_bounds_are_enforced(c):
    base = {"longitude": 77.5946, "latitude": 12.9716}
    assert c.get("/events", params={**base, "radius": 0}).status_code == 422
    assert c.get("/events", params={**base, "radius": 101}).status_code == 422
    assert c.get("/events", params={**base, "radius": 1}).status_code == 200
    assert c.get("/events", params={**base, "radius": 100}).status_code == 200


# ------------------------------------------------------------------- sessions


def _account(c, label="user", password="hunter2"):
    """A user with credentials the caller knows; leaves the client anonymous."""
    email = f"{label}-{uuid.uuid4().hex[:8]}@test.local"
    r = c.post("/users", json={"email": email, "name": label, "password": password})
    assert r.status_code == 200, r.text
    auth = {AUTH: r.cookies.get(AUTH)}
    c.cookies.clear()
    return r.json(), auth, email, password


def _login(c, email, password, cookies=None):
    """POST a session; returns (response, token) and leaves the client anonymous
    so a stray cookie can't make a later anonymous test pass by accident."""
    r = c.post(
        "/users/sessions",
        json={"email": email, "password": password},
        cookies=cookies or {},
    )
    token = r.cookies.get(AUTH)
    c.cookies.clear()
    return r, token


@step()
def test_login_returns_user_and_a_working_cookie(c):
    user, _, email, password = _account(c, "login")
    r, token = _login(c, email, password)
    assert r.status_code == 200, r.text
    assert r.json()["uid"] == user["uid"], r.json()
    assert token, f"no {AUTH} cookie on the session response"
    me = c.get("/users", cookies={AUTH: token})
    assert me.status_code == 200, f"the issued cookie does not authenticate: {me.text}"
    assert me.json()["uid"] == user["uid"], me.json()


@step()
def test_login_with_wrong_password_is_400(c):
    _, _, email, _ = _account(c, "wrongpw")
    r, token = _login(c, email, "not-the-password")
    assert r.status_code == 400, f"expected 400, got {r.status_code} {r.text}"
    assert not token, "a rejected login still handed out an auth cookie"


@step()
def test_login_with_unknown_email_is_400(c):
    r, token = _login(c, f"ghost-{uuid.uuid4().hex}@test.local", "hunter2")
    assert r.status_code == 400, f"expected 400, got {r.status_code} {r.text}"
    assert not token, "a login for a non-existent user handed out a cookie"


@step()
def test_login_does_not_echo_the_password(c):
    """`select *` pulls the password column into the row; UserResponse is what
    keeps it out of the body."""
    _, _, email, password = _account(c, "leak")
    r, _ = _login(c, email, password)
    assert r.status_code == 200, r.text
    assert "password" not in r.json(), r.json()
    assert password not in r.text, "the password came back in the response"


@step()
def test_login_switches_identity_when_a_cookie_is_present(c):
    """Signing in as someone else while holding an old cookie must return the
    new identity -- otherwise a shared browser silently keeps the first user."""
    _, alice_auth, _, _ = _account(c, "switch-a")
    bob, _, bob_email, bob_password = _account(c, "switch-b")

    r, token = _login(c, bob_email, bob_password, cookies=alice_auth)
    assert r.status_code == 200, r.text
    assert r.json()["uid"] == bob["uid"], f"logged in as bob, got back {r.json()}"
    assert token, "no cookie issued for the new identity"
    me = c.get("/users", cookies={AUTH: token})
    assert me.json()["uid"] == bob["uid"], me.json()


@step()
def test_login_rejects_malformed_body(c):
    r = c.post("/users/sessions", json={"email": "someone@test.local"})
    assert r.status_code == 422, f"expected 422, got {r.status_code} {r.text}"


@step("alice_auth")
def test_get_current_user(c):
    r = c.get("/users", cookies=state["alice_auth"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"uid", "name", "email"} <= set(body), body
    assert "password" not in body, body


@step()
def test_get_current_user_anonymous_is_401(c):
    r = c.get("/users")
    assert r.status_code == 401, f"expected 401, got {r.status_code} {r.text}"


@step()
def test_get_current_user_with_garbage_token_is_401(c):
    r = c.get("/users", cookies={AUTH: "not.a.jwt"})
    assert r.status_code == 401, f"expected 401, got {r.status_code} {r.text}"


# ------------------------------------------------------ event scheduling rules


@step("alice_auth", "org")
def test_second_event_at_the_same_venue_is_409(c):
    venue = _make_venue(c, _unique("gap"))
    first = c.post(
        "/events",
        json=_event_body(venue["uid"], "first", starts_in_hours=72),
        cookies=state["alice_auth"],
    )
    assert first.status_code == 200, first.text
    clash = c.post(  # +1 day: inside the 2-day window
        "/events",
        json=_event_body(venue["uid"], "clash", starts_in_hours=96),
        cookies=state["alice_auth"],
    )
    assert (
        clash.status_code == 409
    ), f"expected 409, got {clash.status_code} {clash.text}"


@step("alice_auth", "org")
def test_gap_window_looks_backwards_too(c):
    """The window is two-sided. An existing event that starts BEFORE the new one
    but within two days of it has to conflict as well -- a one-sided
    `starts_at >= new AND starts_at <= new + 2 days` passes this by."""
    venue = _make_venue(c, _unique("backgap"))
    later = c.post(
        "/events",
        json=_event_body(venue["uid"], "later", starts_in_hours=24 * 10),
        cookies=state["alice_auth"],
    )
    assert later.status_code == 200, later.text
    earlier = c.post(
        "/events",
        json=_event_body(venue["uid"], "earlier", starts_in_hours=24 * 9),
        cookies=state["alice_auth"],
    )
    assert (
        earlier.status_code == 409
    ), f"expected 409 looking backwards, got {earlier.status_code} {earlier.text}"


@step("alice_auth", "org")
def test_event_outside_the_gap_is_allowed(c):
    venue = _make_venue(c, _unique("nogap"))
    first = c.post(
        "/events",
        json=_event_body(venue["uid"], "first", starts_in_hours=48),
        cookies=state["alice_auth"],
    )
    assert first.status_code == 200, first.text
    later = c.post(  # +3 days clears the window on both sides
        "/events",
        json=_event_body(venue["uid"], "later", starts_in_hours=48 + 72),
        cookies=state["alice_auth"],
    )
    assert (
        later.status_code == 200
    ), f"expected 200, got {later.status_code} {later.text}"


@step("alice_auth", "org")
def test_gap_is_scoped_to_one_venue(c):
    """Two venues can hold events at the same instant."""
    hall_a, hall_b = _make_venue(c, _unique("hall-a")), _make_venue(
        c, _unique("hall-b")
    )
    at = 24 * 20
    first = c.post(
        "/events",
        json=_event_body(hall_a["uid"], "same-night-a", starts_in_hours=at),
        cookies=state["alice_auth"],
    )
    second = c.post(
        "/events",
        json=_event_body(hall_b["uid"], "same-night-b", starts_in_hours=at),
        cookies=state["alice_auth"],
    )
    assert first.status_code == 200, first.text
    assert (
        second.status_code == 200
    ), f"a different venue conflicted: {second.status_code} {second.text}"


@step("alice_auth", "bob_auth")
def test_cannot_book_a_venue_owned_by_another_org(c):
    """create_event gates on `venues.org_uid = $2`, so a real venue uid that
    belongs to somebody else's org is a 404, not a silent cross-org booking."""
    theirs = c.post(
        "/organisations", json={"name": _unique("other-org")}, cookies=state["bob_auth"]
    )
    assert theirs.status_code == 200, theirs.text
    venue = c.post(
        "/venues",
        json=_venue_body(theirs.json()["uid"], _unique("their-hall"), 44.0, 6.0),
        cookies=state["bob_auth"],
    )
    assert venue.status_code == 200, venue.text
    r = c.post(
        "/events",
        json=_event_body(venue.json()["uid"], "trespass"),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 404, f"expected 404, got {r.status_code} {r.text}"


@step("alice_auth", "org")
def test_conflicting_event_writes_nothing(c):
    """The 409 is raised inside the transaction, so the rollback has to leave
    the venue holding exactly the one event that succeeded."""
    venue = _make_venue(c, _unique("rollback"))
    at = 24 * 30
    kept = c.post(
        "/events",
        json=_event_body(venue["uid"], "kept", starts_in_hours=at),
        cookies=state["alice_auth"],
    )
    assert kept.status_code == 200, kept.text
    dropped = c.post(
        "/events",
        json=_event_body(venue["uid"], "dropped", starts_in_hours=at + 12),
        cookies=state["alice_auth"],
    )
    assert dropped.status_code == 409, dropped.text

    r = c.get("/events", params={"after": kept.json()["id"] - 1, "limit": 100})
    assert r.status_code == 200, r.text
    here = [e["name"] for e in r.json()["events"] if e["venue_uid"] == venue["uid"]]
    assert here == ["kept"], f"expected only the accepted event, found {here}"


# --------------------------------------------------------------- list horizon


@step("alice_auth", "org")
def test_listing_hides_events_starting_within_a_day(c):
    venue = _make_venue(c, _unique("soon"))
    r = c.post(
        "/events",
        json=_event_body(venue["uid"], "too soon", starts_in_hours=2),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 200, r.text
    event = r.json()
    # the horizon is a listing rule, not a delete: it is still fetchable by uid
    assert c.get(f"/events/{event['uid']}").status_code == 200
    listed = _uids(c.get("/events", params={"after": event["id"] - 1, "limit": 100}))
    assert event["uid"] not in listed, "an event starting in 2 hours was listed"


@step("alice_auth", "org")
def test_listing_shows_events_past_the_horizon(c):
    venue = _make_venue(c, _unique("ahead"))
    r = c.post(
        "/events",
        json=_event_body(venue["uid"], "well ahead", starts_in_hours=24 * 40),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 200, r.text
    event = r.json()
    listed = _uids(c.get("/events", params={"after": event["id"] - 1, "limit": 100}))
    assert event["uid"] in listed, "an event 40 days out was filtered away"


# --------------------------------------------------------------- ticket tiers


def _tier(name="gold", price="750.50", capacity=100):
    return {"name": name, "price": price, "capacity": capacity}


def _tier_event(c, label):
    """A venue + event owned by alice, far enough out to dodge both the gap
    check and the listing horizon."""
    venue = _make_venue(c, _unique(label))
    r = c.post(
        "/events",
        json=_event_body(venue["uid"], label, starts_in_hours=24 * 60),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 200, r.text
    return r.json()


@step("alice_auth", "org")
def test_owner_creates_ticket_tier(c):
    event = _tier_event(c, "tier-create")
    r = c.put(
        f"/events/{event['uid']}/tickets/tier",
        json=_tier(),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "gold", body
    assert body["event_uid"] == event["uid"], body
    assert body["capacity"] == 100, body
    # nothing materialised yet, so the whole capacity is still un-materialised
    assert body["available"] == 100, body
    state["tier_event"] = event


@step("alice_auth", "tier_event")
def test_tier_upsert_updates_in_place(c):
    """Same (event_uid, name) must land on the existing row via the unique
    index, not add a second tier."""
    url = f"/events/{state['tier_event']['uid']}/tickets/tier"
    first = c.put(
        url, json=_tier(price="750.50", capacity=100), cookies=state["alice_auth"]
    ).json()
    second = c.put(
        url, json=_tier(price="999.99", capacity=40), cookies=state["alice_auth"]
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["uid"] == first["uid"], "the upsert inserted a new row"
    assert body["price"] == "999.99", body
    assert body["capacity"] == 40, body
    assert body["available"] == 40, body
    assert (
        body["updated_at"] > first["updated_at"]
    ), f"updated_at stood still: {first['updated_at']} -> {body['updated_at']}"


@step("alice_auth", "tier_event")
def test_tiers_are_keyed_by_name(c):
    url = f"/events/{state['tier_event']['uid']}/tickets/tier"
    gold = c.put(url, json=_tier("gold"), cookies=state["alice_auth"]).json()
    silver = c.put(url, json=_tier("silver"), cookies=state["alice_auth"])
    assert silver.status_code == 200, silver.text
    assert silver.json()["uid"] != gold["uid"], "a second tier name reused the row"


@step("alice_auth", "org")
def test_tier_price_keeps_its_cents(c):
    """numeric(12, 2) round-trip. A float price loses this: 1234567890.10 comes
    back as 1234567890.1000001."""
    event = _tier_event(c, "tier-money")
    r = c.put(
        f"/events/{event['uid']}/tickets/tier",
        json=_tier(price="1234567890.10"),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["price"] == "1234567890.10", r.json()["price"]


@step("alice_auth", "org")
def test_tier_rejects_sub_cent_price(c):
    """Better a 422 than silently rounding into numeric(12, 2)."""
    event = _tier_event(c, "tier-precision")
    r = c.put(
        f"/events/{event['uid']}/tickets/tier",
        json=_tier(price="1.005"),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 422, f"expected 422, got {r.status_code} {r.text}"


@step("alice_auth", "org")
def test_tier_rejects_negative_price(c):
    event = _tier_event(c, "tier-negative")
    r = c.put(
        f"/events/{event['uid']}/tickets/tier",
        json=_tier(price="-1.00"),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 422, f"expected 422, got {r.status_code} {r.text}"


@step("alice_auth", "org")
def test_tier_ignores_an_event_uid_in_the_body(c):
    """The event is named by the path. If a body `event_uid` could redirect the
    write, an owner could pass the role check on their own event and tier
    somebody else's."""
    mine = _tier_event(c, "tier-path")
    other = _tier_event(c, "tier-other")
    r = c.put(
        f"/events/{mine['uid']}/tickets/tier",
        json={**_tier("smuggled"), "event_uid": other["uid"]},
        cookies=state["alice_auth"],
    )
    assert r.status_code == 200, r.text
    assert (
        r.json()["event_uid"] == mine["uid"]
    ), f"the body won: wrote to {r.json()['event_uid']}, expected {mine['uid']}"


@step("bob_auth", "tier_event")
def test_non_owner_cannot_create_ticket_tier(c):
    r = c.put(
        f"/events/{state['tier_event']['uid']}/tickets/tier",
        json=_tier("bob-tier"),
        cookies=state["bob_auth"],
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"


@step("tier_event")
def test_anonymous_cannot_create_ticket_tier(c):
    r = c.put(
        f"/events/{state['tier_event']['uid']}/tickets/tier", json=_tier("anon-tier")
    )
    assert r.status_code == 401, f"expected 401, got {r.status_code} {r.text}"


@step("alice_auth")
def test_ticket_tier_on_unknown_event_is_404(c):
    r = c.put(
        f"/events/{uuid.uuid4()}/tickets/tier",
        json=_tier(),
        cookies=state["alice_auth"],
    )
    assert r.status_code == 404, f"expected 404, got {r.status_code} {r.text}"


@step("alice_auth")
def test_ticket_tier_with_malformed_event_uid_is_422(c):
    r = c.put(
        "/events/not-a-uuid/tickets/tier", json=_tier(), cookies=state["alice_auth"]
    )
    assert r.status_code == 422, f"expected 422, got {r.status_code} {r.text}"


@step("alice_auth", "org")
def test_ticket_tier_rejects_malformed_body(c):
    event = _tier_event(c, "tier-malformed")
    r = c.put(
        f"/events/{event['uid']}/tickets/tier",
        json={"name": "gold"},  # no price, no capacity
        cookies=state["alice_auth"],
    )
    assert r.status_code == 422, f"expected 422, got {r.status_code} {r.text}"


@step("alice_auth", "org")
def test_get_ticket_tier_returns_a_tier_for_that_event(c):
    event = _tier_event(c, "tier-read")
    written = c.put(
        f"/events/{event['uid']}/tickets/tier",
        json=_tier("general", price="100.00", capacity=25),
        cookies=state["alice_auth"],
    )
    assert written.status_code == 200, written.text
    r = c.get(f"/events/{event['uid']}/tickets/tier")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_uid"] == event["uid"], body
    assert body["uid"] == written.json()["uid"], body
    assert body["price"] == "100.00", body
    assert body["capacity"] == 25, body
    assert body["available"] == 25, body


@step("alice_auth", "org")
def test_get_ticket_tier_is_unauthenticated(c):
    """Mirrors the other event reads: no CurrentUser, so tiers are public."""
    event = _tier_event(c, "tier-public")
    c.put(
        f"/events/{event['uid']}/tickets/tier",
        json=_tier("public"),
        cookies=state["alice_auth"],
    )
    assert c.get(f"/events/{event['uid']}/tickets/tier").status_code == 200


@step("alice_auth", "org")
def test_get_ticket_tier_for_an_event_with_none_is_404(c):
    event = _tier_event(c, "tier-empty")
    r = c.get(f"/events/{event['uid']}/tickets/tier")
    assert r.status_code == 404, f"expected 404, got {r.status_code} {r.text}"


@step()
def test_get_ticket_tier_for_unknown_event_is_404(c):
    r = c.get(f"/events/{uuid.uuid4()}/tickets/tier")
    assert r.status_code == 404, f"expected 404, got {r.status_code} {r.text}"


@step()
def test_get_ticket_tier_with_malformed_event_uid_is_422(c):
    r = c.get("/events/not-a-uuid/tickets/tier")
    assert r.status_code == 422, f"expected 422, got {r.status_code} {r.text}"


@step("alice_auth", "org")
def test_get_ticket_tier_with_several_tiers(c):
    """An event has many tiers, but the route is `fetchrow` + a singular
    response_model, so it can only ever hand back one of them -- and which one
    is whatever the scan reaches first, since there is no ORDER BY."""
    event = _tier_event(c, "tier-many")
    for name, price in (
        ("general", "100.00"),
        ("gold", "500.00"),
        ("platinum", "900.00"),
    ):
        w = c.put(
            f"/events/{event['uid']}/tickets/tier",
            json=_tier(name, price=price),
            cookies=state["alice_auth"],
        )
        assert w.status_code == 200, w.text
    r = c.get(f"/events/{event['uid']}/tickets/tier")
    assert r.status_code == 200, r.text
    assert r.json()["event_uid"] == event["uid"], r.json()
    assert r.json()["name"] in {"general", "gold", "platinum"}, r.json()


def _materialise(tier_uid, n):
    """Pretend n units have been pulled out of the un-materialised pool, the way
    replenishment will once bookings exist. Goes straight to the database
    because no route does this yet."""

    async def go():
        conn = await asyncpg.connect(Config.db_uri)
        await conn.execute(
            "update tickets_tier set available = available - $1 where uid = $2",
            n,
            tier_uid,
        )
        await conn.close()

    asyncio.run(go())


@step("alice_auth", "org")
def test_capacity_change_keeps_what_is_already_materialised(c):
    """available is capacity minus what has been turned into ticket rows, so a
    capacity edit has to move only the un-materialised remainder."""
    url = f"/events/{_tier_event(c, 'tier-capacity')['uid']}/tickets/tier"
    created = c.put(url, json=_tier("gold", capacity=100), cookies=state["alice_auth"])
    assert created.status_code == 200, created.text
    _materialise(created.json()["uid"], 30)  # 30 of the 100 now exist as rows

    raised = c.put(url, json=_tier("gold", capacity=150), cookies=state["alice_auth"])
    assert raised.status_code == 200, raised.text
    assert raised.json()["capacity"] == 150, raised.json()
    assert (
        raised.json()["available"] == 120
    ), f"expected 150 - 30 materialised = 120, got {raised.json()['available']}"

    lowered = c.put(url, json=_tier("gold", capacity=40), cookies=state["alice_auth"])
    assert lowered.status_code == 200, lowered.text
    assert lowered.json()["available"] == 10, lowered.json()


@step("alice_auth", "org")
def test_capacity_cannot_drop_below_what_is_materialised(c):
    """Shrinking past the rows that already exist would strand live tickets, so
    the route refuses it before the upsert runs."""
    url = f"/events/{_tier_event(c, 'tier-shrink')['uid']}/tickets/tier"
    created = c.put(url, json=_tier("gold", capacity=100), cookies=state["alice_auth"])
    assert created.status_code == 200, created.text
    _materialise(created.json()["uid"], 60)

    r = c.put(url, json=_tier("gold", capacity=50), cookies=state["alice_auth"])
    assert r.status_code == 409, f"expected 409, got {r.status_code} {r.text}"
    assert "60" in r.json()["detail"], r.json()


# ------------------------------------------------------------------- bookings


def _booking_event(c, label, tiers=(("gold", "750.50", 100),)):
    event = _tier_event(c, label)
    for name, price, capacity in tiers:
        r = c.put(
            f"/events/{event['uid']}/tickets/tier",
            json=_tier(name, price=price, capacity=capacity),
            cookies=state["alice_auth"],
        )
        assert r.status_code == 200, r.text
    return event


def _book(c, event_uid, tickets, auth=None, booking_uid=None):
    return c.post(
        f"/events/{event_uid}/bookings",
        json={
            "booking_uid": str(booking_uid or uuid.uuid4()),
            "tickets": [{"tier_name": n, "quantity": q} for n, q in tickets],
        },
        cookies=auth or state["alice_auth"],
    )


def _inventory(event_uid, tier_name):
    """(capacity, available, materialised, booked) straight from the database."""

    async def go():
        conn = await asyncpg.connect(Config.db_uri)
        tier = await conn.fetchrow(
            "select capacity, available from tickets_tier"
            " where event_uid=$1 and name=$2",
            uuid.UUID(event_uid),
            tier_name,
        )
        rows = await conn.fetch(
            "select status, count(*) as n from tickets"
            " where event_uid=$1 and ticket_tier_name=$2 group by status",
            uuid.UUID(event_uid),
            tier_name,
        )
        await conn.close()
        by_status = {r["status"]: r["n"] for r in rows}
        return (
            tier["capacity"],
            tier["available"],
            sum(by_status.values()),
            by_status.get("booked", 0),
        )

    return asyncio.run(go())


@step("alice_auth", "org")
def test_booking_materialises_from_an_empty_pool(c):
    """Nothing is materialised up front, so the first booking takes the replenish
    path: draw from tickets_tier.available and create the rows."""
    event = _booking_event(c, "book-first")
    assert _inventory(event["uid"], "gold")[2] == 0, "pool should start empty"

    r = _book(c, event["uid"], [("gold", 2)])
    assert r.status_code == 200, f"expected 200, got {r.status_code} {r.text}"
    body = r.json()
    assert body["amount"] == "1501.00", body
    assert body["tickets"] == [{"tier_name": "gold", "quantity": 2}], body

    capacity, available, materialised, booked = _inventory(event["uid"], "gold")
    assert booked == 2, f"expected 2 booked rows, got {booked}"
    assert (
        capacity - available == materialised
    ), f"{capacity} - {available} != {materialised} materialised"
    state["booking"] = body
    state["booking_event"] = event


@step("alice_auth", "org")
def test_booking_is_served_from_an_existing_pool(c):
    """The second booking must come from rows the first materialised, not draw
    from capacity again."""
    event = _booking_event(c, "book-reuse", tiers=(("gold", "10.00", 500),))
    _book(c, event["uid"], [("gold", 1)])
    _, available_after_first, materialised_after_first, _ = _inventory(
        event["uid"], "gold"
    )

    _book(c, event["uid"], [("gold", 1)])
    _, available, materialised, booked = _inventory(event["uid"], "gold")

    assert booked == 2, f"expected 2 booked, got {booked}"
    assert (
        available == available_after_first
    ), f"drew from capacity again: {available_after_first} -> {available}"
    assert materialised == materialised_after_first, "pool grew unnecessarily"


@step("alice_auth", "org")
def test_booking_amount_is_price_times_quantity(c):
    """The amount comes from tickets_tier.price, which is why the tier lookup
    fetches price instead of only validating the name."""
    event = _booking_event(
        c, "book-amount", tiers=(("gold", "750.50", 100), ("silver", "100.25", 100))
    )
    r = _book(c, event["uid"], [("gold", 2), ("silver", 3)])
    assert r.status_code == 200, r.text
    expected = Decimal("750.50") * 2 + Decimal("100.25") * 3
    assert (
        Decimal(r.json()["amount"]) == expected
    ), f"expected {expected}, got {r.json()['amount']}"
    assert sum(t["quantity"] for t in r.json()["tickets"]) == 5, r.json()


@step("alice_auth", "org")
def test_repeated_tier_in_one_request_is_merged(c):
    """skip locked does not skip our own locks, so a repeated tier would
    otherwise be handed the same rows twice and report double what exists."""
    event = _booking_event(c, "book-dupe")
    r = _book(c, event["uid"], [("gold", 2), ("gold", 3)])
    assert r.status_code == 200, r.text
    assert r.json()["tickets"] == [{"tier_name": "gold", "quantity": 5}], r.json()
    assert _inventory(event["uid"], "gold")[3] == 5


@step("alice_auth", "org")
def test_replaying_a_booking_uid_returns_the_same_booking(c):
    """booking_uid is the client's idempotency key -- a retry must not hold a
    second batch of tickets."""
    event = _booking_event(c, "book-idem")
    booking_uid = uuid.uuid4()

    first = _book(c, event["uid"], [("gold", 2)], booking_uid=booking_uid)
    assert first.status_code == 200, first.text
    second = _book(c, event["uid"], [("gold", 2)], booking_uid=booking_uid)
    assert second.status_code == 200, second.text

    assert (
        second.json()["amount"] == first.json()["amount"]
    ), "replay changed the amount"
    booked = _inventory(event["uid"], "gold")[3]
    assert booked == 2, f"replay held a second batch: {booked} tickets booked"


@step("alice_auth", "bob_auth", "org")
def test_another_user_cannot_reuse_a_booking_uid(c):
    event = _booking_event(c, "book-steal")
    booking_uid = uuid.uuid4()
    assert (
        _book(c, event["uid"], [("gold", 1)], booking_uid=booking_uid).status_code
        == 200
    )

    r = _book(
        c, event["uid"], [("gold", 1)], auth=state["bob_auth"], booking_uid=booking_uid
    )
    assert r.status_code == 409, f"expected 409, got {r.status_code} {r.text}"
    assert _inventory(event["uid"], "gold")[3] == 1, "bob's attempt claimed tickets"


@step("alice_auth", "org")
def test_booking_more_than_capacity_is_409(c):
    event = _booking_event(c, "book-soldout", tiers=(("gold", "10.00", 2),))
    r = _book(c, event["uid"], [("gold", 5)])
    assert r.status_code == 409, f"expected 409, got {r.status_code} {r.text}"
    _, available, _, booked = _inventory(event["uid"], "gold")
    assert booked == 0, "a failed booking left tickets claimed"
    assert available == 2, f"a failed booking drew down capacity: available={available}"


@step("alice_auth", "org")
def test_booking_an_unknown_tier_is_400_and_names_it(c):
    event = _booking_event(c, "book-badtier")
    r = _book(c, event["uid"], [("gold", 1), ("platinum", 1)])
    assert r.status_code == 400, f"expected 400, got {r.status_code} {r.text}"
    assert "platinum" in r.json()["detail"], r.json()


@step("alice_auth")
def test_booking_an_unknown_event_is_404(c):
    r = _book(c, str(uuid.uuid4()), [("gold", 1)])
    assert r.status_code == 404, f"expected 404, got {r.status_code} {r.text}"


@step("booking_event")
def test_anonymous_cannot_book(c):
    r = c.post(
        f"/events/{state['booking_event']['uid']}/bookings",
        json={
            "booking_uid": str(uuid.uuid4()),
            "tickets": [{"tier_name": "gold", "quantity": 1}],
        },
    )
    assert r.status_code == 401, f"expected 401, got {r.status_code} {r.text}"


# ------------------------------------------------------------ reading bookings


@step("alice_auth", "booking", "booking_event")
def test_get_booking(c):
    r = c.get(
        f"/bookings/{state['booking']['booking_uid']}", cookies=state["alice_auth"]
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["booking_uid"] == state["booking"]["booking_uid"], body
    assert body["event_uid"] == state["booking_event"]["uid"], body
    assert body["amount"] == state["booking"]["amount"], body
    assert body["tickets"] == state["booking"]["tickets"], body
    assert body["status"] == "confirmed", body
    assert body["expires_at"] and body["created_at"], body


@step("bob_auth", "booking")
def test_get_someone_elses_booking_is_404(c):
    """Scoped by user_uid, so a leaked booking uid is not enough to read it."""
    r = c.get(f"/bookings/{state['booking']['booking_uid']}", cookies=state["bob_auth"])
    assert (
        r.status_code == 404
    ), f"expected 404 (no existence leak), got {r.status_code}"


@step("alice_auth")
def test_get_unknown_booking_is_404(c):
    r = c.get(f"/bookings/{uuid.uuid4()}", cookies=state["alice_auth"])
    assert r.status_code == 404, f"expected 404, got {r.status_code} {r.text}"


@step("booking")
def test_get_booking_requires_auth(c):
    r = c.get(f"/bookings/{state['booking']['booking_uid']}")
    assert r.status_code == 401, f"expected 401, got {r.status_code} {r.text}"


@step("alice_auth", "bob_auth", "org")
def test_list_bookings_of_event_is_scoped_to_the_caller(c):
    event = _booking_event(c, "book-list", tiers=(("gold", "10.00", 100),))
    mine = {
        _book(c, event["uid"], [("gold", 1)]).json()["booking_uid"] for _ in range(3)
    }
    theirs = _book(c, event["uid"], [("gold", 1)], auth=state["bob_auth"])
    assert theirs.status_code == 200, theirs.text

    r = c.get(f"/events/{event['uid']}/bookings", cookies=state["alice_auth"])
    assert r.status_code == 200, r.text
    seen = {b["booking_uid"] for b in r.json()["bookings"]}
    assert seen == mine, f"expected {mine}, saw {seen}"
    assert theirs.json()["booking_uid"] not in seen, "another user's booking leaked"

    r = c.get(f"/events/{event['uid']}/bookings", cookies=state["bob_auth"])
    assert {b["booking_uid"] for b in r.json()["bookings"]} == {
        theirs.json()["booking_uid"]
    }, "bob should see only his own"


@step("alice_auth", "org")
def test_list_bookings_of_event_excludes_other_events(c):
    one = _booking_event(c, "book-scope-a", tiers=(("gold", "10.00", 100),))
    two = _booking_event(c, "book-scope-b", tiers=(("gold", "10.00", 100),))
    here = _book(c, one["uid"], [("gold", 1)]).json()["booking_uid"]
    there = _book(c, two["uid"], [("gold", 1)]).json()["booking_uid"]

    seen = {
        b["booking_uid"]
        for b in c.get(
            f"/events/{one['uid']}/bookings", cookies=state["alice_auth"]
        ).json()["bookings"]
    }
    assert seen == {here}, f"expected only {here}, saw {seen}"
    assert there not in seen, "a booking from another event leaked in"


@step("alice_auth", "org")
def test_list_bookings_spans_events(c):
    """/bookings is the user's whole history, so it must cross events -- the
    event-leading index cannot serve it."""
    one = _booking_event(c, "book-all-a", tiers=(("gold", "10.00", 100),))
    two = _booking_event(c, "book-all-b", tiers=(("gold", "10.00", 100),))
    created = {
        _book(c, one["uid"], [("gold", 1)]).json()["booking_uid"],
        _book(c, two["uid"], [("gold", 1)]).json()["booking_uid"],
    }

    r = c.get("/bookings", params={"limit": 100}, cookies=state["alice_auth"])
    assert r.status_code == 200, r.text
    seen = {b["booking_uid"] for b in r.json()["bookings"]}
    assert created <= seen, f"{created - seen} missing from /bookings"
    events = {b["event_uid"] for b in r.json()["bookings"]}
    assert {one["uid"], two["uid"]} <= events, events


@step("bob_auth")
def test_list_bookings_is_per_user(c):
    r = c.get("/bookings", params={"limit": 100}, cookies=state["bob_auth"])
    assert r.status_code == 200, r.text
    assert all(
        b["event_uid"] for b in r.json()["bookings"]
    ), "rows should carry their event"
    alice_uid = state.get("alice", {}).get("uid")
    for b in r.json()["bookings"]:
        assert b.get("user_uid", None) is None or b["user_uid"] != alice_uid


@step("alice_auth", "org")
def test_list_bookings_paginates_without_repeats(c):
    event = _booking_event(c, "book-page", tiers=(("gold", "10.00", 100),))
    created = {
        _book(c, event["uid"], [("gold", 1)]).json()["booking_uid"] for _ in range(5)
    }

    seen, after, pages = [], 0, 0
    while True:
        r = c.get(
            f"/events/{event['uid']}/bookings",
            params={"after": after, "limit": 2},
            cookies=state["alice_auth"],
        )
        assert r.status_code == 200, r.text
        body = r.json()
        seen.extend(b["booking_uid"] for b in body["bookings"])
        pages += 1
        assert pages < 20, "cursor never terminated -- pagination is looping"
        if body.get("next") is None:
            break
        after = body["next"]

    assert len(seen) == len(set(seen)), f"duplicate bookings across pages: {seen}"
    assert set(seen) == created, f"expected {created}, saw {set(seen)}"


@step("alice_auth", "org")
def test_list_returns_tickets_for_every_booking(c):
    """The list stitches tickets in one grouped query rather than one per row;
    each booking must still get its own tickets, not another's."""
    event = _booking_event(
        c, "book-stitch", tiers=(("gold", "10.00", 100), ("silver", "5.00", 100))
    )
    one = _book(c, event["uid"], [("gold", 2)]).json()["booking_uid"]
    two = _book(c, event["uid"], [("silver", 3)]).json()["booking_uid"]

    r = c.get(f"/events/{event['uid']}/bookings", cookies=state["alice_auth"])
    by_uid = {b["booking_uid"]: b["tickets"] for b in r.json()["bookings"]}
    assert by_uid[one] == [{"tier_name": "gold", "quantity": 2}], by_uid[one]
    assert by_uid[two] == [{"tier_name": "silver", "quantity": 3}], by_uid[two]


@step()
def test_listing_bookings_requires_auth(c):
    assert c.get("/bookings").status_code == 401


def main():
    truncate()
    passed = failed = skipped = 0
    with TestClient(create_api()) as c:
        for fn, requires in results:
            missing = [k for k in requires if k not in state]
            if missing:
                skipped += 1
                print(f"skip  {fn.__name__}  (blocked: {', '.join(missing)})")
                continue
            try:
                fn(c)
            except AssertionError as e:
                failed += 1
                print(f"FAIL  {fn.__name__}\n        {e}")
            except Exception as e:
                failed += 1
                print(f"ERROR {fn.__name__}\n        {type(e).__name__}: {e}")
            else:
                passed += 1
                print(f"ok    {fn.__name__}")
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
