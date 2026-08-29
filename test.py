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

import asyncpg
from fastapi.testclient import TestClient

from src import create_api
from src.config import Config

TABLES = ["tickets", "bookings", "tickets_tier", "events", "venues",
          "memberships", "organisations", "users"]

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
        existing = [r["relname"] for r in await conn.fetch(
            "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname='public' and c.relkind='r'")]
        targets = [t for t in TABLES if t in existing]
        if targets:
            await conn.execute(f"truncate {', '.join(targets)} restart identity cascade")
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
    payload = {"email": f"dup-{uuid.uuid4().hex[:8]}@test.local", "name": "dup", "password": "x"}
    first = c.post("/users", json=payload)
    c.cookies.clear()
    assert first.status_code == 200, first.text
    second = c.post("/users", json=payload)
    c.cookies.clear()
    assert second.status_code == 400, f"expected 400, got {second.status_code} {second.text}"


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
    assert r.status_code == 404, f"expected 404 (no existence leak), got {r.status_code} {r.text}"


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
    r = c.post(f"/organisations/{state['org']['uid']}/members",
               json={"members": [{"user_uid": state["bob"]["uid"], "role": "member"}]},
               cookies=state["bob_auth"])
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"


@step("alice_auth", "bob", "org")
def test_owner_adds_member(c):
    r = c.post(f"/organisations/{state['org']['uid']}/members",
               json={"members": [{"user_uid": state["bob"]["uid"], "role": "member"}]},
               cookies=state["alice_auth"])
    assert r.status_code == 200, r.text


@step("bob_auth", "org")
def test_new_member_can_now_see_org(c):
    r = c.get(f"/organisations/{state['org']['uid']}", cookies=state["bob_auth"])
    assert r.status_code == 200, f"bob is a member now, got {r.status_code} {r.text}"
    assert r.json()["role"] == "member", r.json()


@step("alice", "alice_auth", "bob", "org")
def test_list_members(c):
    r = c.get(f"/organisations/{state['org']['uid']}/members", cookies=state["alice_auth"])
    assert r.status_code == 200, r.text
    by_uid = {m["user_uid"]: m["role"] for m in r.json()["members"]}
    assert by_uid.get(state["alice"]["uid"]) == "owner", by_uid
    assert by_uid.get(state["bob"]["uid"]) == "member", by_uid


@step("alice_auth", "bob", "org")
def test_adding_same_member_twice_is_not_a_500(c):
    r = c.post(f"/organisations/{state['org']['uid']}/members",
               json={"members": [{"user_uid": state["bob"]["uid"], "role": "member"}]},
               cookies=state["alice_auth"])
    assert r.status_code < 500, f"unique(org_uid,user_uid) leaked as {r.status_code}: {r.text}"
    assert r.status_code == 409, f"expected 409 conflict, got {r.status_code}"


# ---------------------------------------------------------------- pagination

@step("alice_auth")
def test_pagination_covers_every_row_exactly_once(c):
    """The point of the cursor: walk every page, see each org exactly once."""
    for i in range(4):                                  # alice ends up with 5 orgs
        r = c.post("/organisations", json={"name": f"page-org-{i}"}, cookies=state["alice_auth"])
        assert r.status_code == 200, r.text

    seen, after, pages = [], 0, 0
    while True:
        r = c.get("/organisations", params={"after": after, "limit": 2},
                  cookies=state["alice_auth"])
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
    r = c.post(f"/organisations/{org_uid}/members",
               json={"members": [{"user_uid": state["bob"]["uid"], "role": "member"}]},
               cookies=state["alice_auth"])
    assert r.status_code == 200, r.text
    return org_uid


def _roles(c, org_uid):
    r = c.get(f"/organisations/{org_uid}/members", cookies=state["alice_auth"])
    assert r.status_code == 200, r.text
    return {m["user_uid"]: m["role"] for m in r.json()["members"]}


@step("alice_auth", "bob")
def test_update_member_role(c):
    org_uid = _org_with_bob(c, "promote-me")
    r = c.put(f"/organisations/{org_uid}/members/{state['bob']['uid']}",
              json={"role": "owner"}, cookies=state["alice_auth"])
    assert r.status_code == 200, r.text
    assert _roles(c, org_uid)[state["bob"]["uid"]] == "owner", "role was not updated"


@step("alice_auth", "bob")
def test_role_update_does_not_leak_across_orgs(c):
    """A role change in one org must not touch the same user's role elsewhere."""
    target = _org_with_bob(c, "leak-target")
    bystander = _org_with_bob(c, "leak-bystander")
    r = c.put(f"/organisations/{target}/members/{state['bob']['uid']}",
              json={"role": "owner"}, cookies=state["alice_auth"])
    assert r.status_code == 200, r.text
    role_elsewhere = _roles(c, bystander)[state["bob"]["uid"]]
    assert role_elsewhere == "member", \
        f"role update leaked: bob became {role_elsewhere!r} in an unrelated org"


@step("alice_auth", "bob")
def test_delete_member(c):
    org_uid = _org_with_bob(c, "kick-me")
    r = c.delete(f"/organisations/{org_uid}/members/{state['bob']['uid']}",
                 cookies=state["alice_auth"])
    assert r.status_code == 204, f"expected 204, got {r.status_code} {r.text}"
    assert state["bob"]["uid"] not in _roles(c, org_uid), "member still present after delete"


@step("alice_auth")
def test_owner_cannot_delete_themselves(c):
    r = c.post("/organisations", json={"name": "self-kick"}, cookies=state["alice_auth"])
    assert r.status_code == 200, r.text
    org_uid = r.json()["uid"]
    r = c.delete(f"/organisations/{org_uid}/members/{state['alice']['uid']}",
                 cookies=state["alice_auth"])
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
    return {"org_uid": org_uid, "name": name,
            "location": {"longitude": longitude, "latitude": latitude}}


def _unique(prefix):
    """unique(name, location) is global, so every test needs its own name."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@step("alice_auth", "org")
def test_owner_creates_venue(c):
    name = _unique("hall")
    r = c.post("/venues", json=_venue_body(state["org"]["uid"], name), cookies=state["alice_auth"])
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
    r = c.post("/venues", json=_venue_body(state["org"]["uid"], _unique("member")),
               cookies=state["bob_auth"])
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"


@step("alice_auth")
def test_cannot_create_venue_in_unknown_org(c):
    """No membership row at all -- must not fall through to the insert."""
    r = c.post("/venues", json=_venue_body(str(uuid.uuid4()), _unique("ghost")),
               cookies=state["alice_auth"])
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"


@step("alice_auth", "org")
def test_duplicate_name_and_location_is_409(c):
    body = _venue_body(state["org"]["uid"], _unique("twin"))
    first = c.post("/venues", json=body, cookies=state["alice_auth"])
    assert first.status_code == 200, first.text
    second = c.post("/venues", json=body, cookies=state["alice_auth"])
    assert second.status_code == 409, \
        f"unique(name, location) should surface as 409, got {second.status_code} {second.text}"
    # the handler must not echo asyncpg's message (it carries the constraint and the WKB)
    assert "venues_name_location_key" not in second.text, second.text


@step("alice_auth", "org")
def test_same_name_different_location_allowed(c):
    name = _unique("twosites")
    a = c.post("/venues", json=_venue_body(state["org"]["uid"], name, 10.0, 10.0),
               cookies=state["alice_auth"])
    b = c.post("/venues", json=_venue_body(state["org"]["uid"], name, 20.0, 20.0),
               cookies=state["alice_auth"])
    assert a.status_code == 200, a.text
    assert b.status_code == 200, f"same name at a different point is a different venue: {b.text}"
    assert a.json()["uid"] != b.json()["uid"]


@step("alice_auth", "org")
def test_same_location_different_name_allowed(c):
    point = (30.0, 30.0)
    a = c.post("/venues", json=_venue_body(state["org"]["uid"], _unique("stadium"), *point),
               cookies=state["alice_auth"])
    b = c.post("/venues", json=_venue_body(state["org"]["uid"], _unique("annex"), *point),
               cookies=state["alice_auth"])
    assert a.status_code == 200, a.text
    assert b.status_code == 200, f"same point under another name is allowed: {b.text}"


@step("alice_auth", "org")
def test_point_precision_still_conflicts(c):
    """POINT(1.5 1.5) and POINT(1.50 1.50) are the same float8 pair."""
    name = _unique("precise")
    a = c.post("/venues", json=_venue_body(state["org"]["uid"], name, 1.5, 1.5),
               cookies=state["alice_auth"])
    assert a.status_code == 200, a.text
    b = c.post("/venues", json=_venue_body(state["org"]["uid"], name, 1.50, 1.50),
               cookies=state["alice_auth"])
    assert b.status_code == 409, f"expected 409, got {b.status_code} {b.text}"


@step("alice_auth", "org")
def test_out_of_range_coordinates_are_422(c):
    for lon, lat in [(181.0, 0.0), (-181.0, 0.0), (0.0, 91.0), (0.0, -91.0)]:
        r = c.post("/venues", json=_venue_body(state["org"]["uid"], _unique("bad"), lon, lat),
                   cookies=state["alice_auth"])
        assert r.status_code == 422, f"({lon}, {lat}) should be rejected, got {r.status_code}"


@step("alice_auth", "org")
def test_create_venue_rejects_malformed_body(c):
    r = c.post("/venues", json={"org_uid": state["org"]["uid"], "name": "no-location"},
               cookies=state["alice_auth"])
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
        r = c.post("/venues", json=_venue_body(state["org"]["uid"], _unique(f"page-{i}"),
                                               100.0 + i, 40.0),
                   cookies=state["alice_auth"])
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
