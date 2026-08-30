"""Seed the dev database with enough rows to make index work measurable.

    python seed.py                      # 10k users, 10k orgs, ~20k memberships
    python seed.py --users 50000 --orgs 50000 --extra-members 100000
    python seed.py --venues 30000 --events 120000
    python seed.py --keep               # append instead of truncating first
    python seed.py --indexes            # also create the geo/join indexes
    python seed.py --drop-indexes       # remove them again, to A/B a plan

Every organisation gets exactly one owner, then `--extra-members` additional
(org, user) pairs are drawn at random, so the membership table has realistic
fan-out rather than a flat 1:1 mapping.

One user -- probe@seed.local -- is deliberately placed in `--probe-orgs`
organisations so `list_organisations` has something to paginate through. Its
uid is printed at the end; use it in EXPLAIN ANALYZE.

Venues are clustered on purpose: `--cluster-share` of them land within ~20 km of
the anchor (Bangalore by default) and the rest are scattered worldwide, so a
proximity search is selective but non-empty -- the case that actually
discriminates between query plans. Move the anchor into an ocean to measure the
empty-result case instead.

Rows are loaded with COPY (asyncpg's copy_records_to_table), which is the
fastest bulk path -- far quicker than executemany for this volume. Venues go
through a temp staging table because asyncpg has no binary codec for PostGIS
geography; the cast to geography happens server-side on the way out.
"""

import argparse
import asyncio
import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import perf_counter

import asyncpg

from src.auth.auth import get_token
from src.config import Config
from src.database.db import load_schemas

PASSWORD = "seeded-not-a-real-password"
PROBE_EMAIL = "probe@seed.local"

# truncate order: children first, so cascade has nothing to chase
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

BOOKING_STATUSES = ["pending", "confirmed", "expired", "cancelled"]
TICKET_STATUSES = ["held", "issued", "cancelled"]
TIER_NAMES = ["general", "gold", "platinum"]

# Bangalore; a venue is "clustered" if it lands within this many degrees.
ANCHOR_LON, ANCHOR_LAT = 77.5946, 12.9716
CLUSTER_DEGREES = 0.2

INDEXES = {
    # ST_DWithin can only use GiST -- the unique btree on (name, location) is no
    # help for proximity.
    "venues_location_gist": "create index venues_location_gist on venues using gist (location)",
    # lets the planner drive the join venues -> events once the geo filter has
    # narrowed venues down. Without it a GiST scan still seq-scans events, which
    # is slower than doing no geo indexing at all.
    "events_venue_uid_idx": "create index events_venue_uid_idx on events (venue_uid)",
}


async def apply_indexes(conn, create: bool):
    for name, sql in INDEXES.items():
        await conn.execute(f"drop index if exists {name}")
        if create:
            await conn.execute(sql)
        print(f"  {'created' if create else 'dropped'}  {name}")


async def seed(args):
    rng = random.Random(args.seed)
    started = perf_counter()
    now = datetime.now(timezone.utc)
    # --keep appends to whatever is there, so generated names need a per-run tag
    # or the second run collides on users.email / venues.name.
    tag = "" if not args.keep else f"-{uuid.uuid4().hex[:6]}"

    await load_schemas()
    conn = await asyncpg.connect(Config.db_uri)

    if args.drop_indexes:
        await apply_indexes(conn, create=False)
        await conn.close()
        return

    if not args.keep:
        await conn.execute(f"truncate {', '.join(TABLES)} restart identity cascade")
        print(f"truncated {', '.join(TABLES)}")

    # ---------------------------------------------------------------- users
    # the probe user is the one stable handle across runs, so under --keep we
    # adopt the existing row instead of inserting a colliding one.
    existing_probe = await conn.fetchval(
        "select uid from users where email=$1", PROBE_EMAIL
    )
    user_uids = [uuid.uuid4() for _ in range(args.users)]
    if existing_probe:
        user_uids[0] = existing_probe
    users = [
        (
            user_uids[i],
            PROBE_EMAIL if i == 0 else f"user{i}{tag}@seed.local",
            "probe" if i == 0 else f"user{i}{tag}",
            PASSWORD,
        )
        for i in range(args.users)
        if not (i == 0 and existing_probe)
    ]
    await conn.copy_records_to_table(
        "users", records=users, columns=["uid", "email", "name", "password"]
    )
    print(f"users          {len(users):>8}" + ("  (probe reused)" if existing_probe else ""))

    # -------------------------------------------------------- organisations
    org_uids = [uuid.uuid4() for _ in range(args.orgs)]
    orgs = [(org_uids[i], f"org-{i}{tag}") for i in range(args.orgs)]
    await conn.copy_records_to_table(
        "organisations", records=orgs, columns=["uid", "name"]
    )
    print(f"organisations  {len(orgs):>8}")

    # ----------------------------------------------------------- memberships
    # (org_index, user_index) pairs -- the set enforces unique(org_uid, user_uid)
    # in Python so COPY never trips the constraint.
    pairs: set[tuple[int, int]] = set()
    rows: list[tuple] = []

    def add(org_i: int, user_i: int, role: str) -> bool:
        if (org_i, user_i) in pairs:
            return False
        pairs.add((org_i, user_i))
        rows.append((uuid.uuid4(), org_uids[org_i], user_uids[user_i], role))
        return True

    for org_i in range(args.orgs):  # one owner per org
        add(org_i, rng.randrange(args.users), "owner")

    probe_target = min(args.probe_orgs, args.orgs)  # a user worth paginating
    placed, org_i = 0, 0
    while placed < probe_target and org_i < args.orgs:
        if add(org_i, 0, "member"):
            placed += 1
        org_i += 1

    attempts, wanted = 0, args.extra_members
    while len(rows) < args.orgs + probe_target + wanted and attempts < wanted * 4:
        add(rng.randrange(args.orgs), rng.randrange(args.users), "member")
        attempts += 1

    await conn.copy_records_to_table(
        "memberships", records=rows, columns=["uid", "org_uid", "user_uid", "role"]
    )
    print(
        f"memberships    {len(rows):>8}  ({args.orgs} owner, {len(rows) - args.orgs} member)"
    )

    # --------------------------------------------------------------- venues
    # unique(name, location), so the index goes in the name to stay collision-free.
    venue_uids = [uuid.uuid4() for _ in range(args.venues)]
    venue_org = [rng.randrange(args.orgs) for _ in range(args.venues)]
    venues, clustered = [], 0
    for i in range(args.venues):
        if rng.random() < args.cluster_share:
            lon = args.lon + rng.uniform(-CLUSTER_DEGREES, CLUSTER_DEGREES)
            lat = args.lat + rng.uniform(-CLUSTER_DEGREES, CLUSTER_DEGREES)
            clustered += 1
        else:
            lon, lat = rng.uniform(-180, 180), rng.uniform(-85, 85)
        venues.append(
            (
                venue_uids[i],
                user_uids[rng.randrange(args.users)],
                org_uids[venue_org[i]],
                f"venue-{i}{tag}",
                f"SRID=4326;POINT({lon} {lat})",
            )
        )

    # asyncpg can't COPY into a geography column (no binary codec for PostGIS),
    # so stage the WKT as text and let the server cast it.
    await conn.execute("drop table if exists venues_stage")
    await conn.execute(
        "create temp table venues_stage ("
        "uid uuid, creator_user_uid uuid, org_uid uuid, name text, location text)"
    )
    await conn.copy_records_to_table(
        "venues_stage",
        records=venues,
        columns=["uid", "creator_user_uid", "org_uid", "name", "location"],
    )
    await conn.execute(
        "insert into venues(uid, creator_user_uid, org_uid, name, location) "
        "select uid, creator_user_uid, org_uid, name, location::geography from venues_stage"
    )
    await conn.execute("drop table venues_stage")
    print(f"venues         {len(venues):>8}  ({clustered} near the anchor)")

    # --------------------------------------------------------------- events
    # org_uid is taken from the venue's own org so the two agree. Nothing in the
    # API enforces that today, but seeded data may as well be coherent.
    event_uids = [uuid.uuid4() for _ in range(args.events)]
    events = []
    for i in range(args.events):
        v = rng.randrange(args.venues)
        starts = now + timedelta(hours=rng.randint(1, 24 * 365))
        events.append(
            (
                event_uids[i],
                f"event-{i}{tag}",
                org_uids[venue_org[v]],
                uuid.uuid4(),  # performer_uid: no performers table yet
                venue_uids[v],
                starts,
                starts + timedelta(hours=rng.randint(1, 6)),
            )
        )
    await conn.copy_records_to_table(
        "events",
        records=events,
        columns=[
            "uid",
            "name",
            "org_uid",
            "performer_uid",
            "venue_uid",
            "starts_at",
            "ends_at",
        ],
    )
    print(f"events         {len(events):>8}")

    # --------------------------------------------------------- tickets_tier
    # only the first --ticketed-events events get tiers, so the ticket tables
    # stay a sane size while events itself stays large.
    ticketed = event_uids[: min(args.ticketed_events, len(event_uids))]
    tiers, tiers_by_event = [], {}
    for event in ticketed:
        tiers_by_event[event] = []
        for j, name in enumerate(TIER_NAMES):
            tier_uid = uuid.uuid4()
            tiers_by_event[event].append(tier_uid)
            tiers.append(
                (tier_uid, name, event, Decimal(500 * (j + 1)), rng.randint(0, 500))
            )
    await conn.copy_records_to_table(
        "tickets_tier",
        records=tiers,
        columns=["uid", "name", "event_uid", "price", "available"],
    )
    print(f"tickets_tier   {len(tiers):>8}")

    # ------------------------------------------------- bookings and tickets
    bookings, tickets = [], []
    for _ in range(args.bookings if ticketed else 0):
        event = ticketed[rng.randrange(len(ticketed))]
        booking_uid = uuid.uuid4()
        status = rng.choice(BOOKING_STATUSES)
        seats = rng.randint(1, 4)
        tier = rng.choice(tiers_by_event[event])
        bookings.append(
            (
                booking_uid,
                Decimal(seats * 750),
                status,
                event,
                now + timedelta(minutes=rng.randint(-60, 60)),
            )
        )
        for _ in range(seats):
            tickets.append(
                (
                    uuid.uuid4(),
                    event,
                    booking_uid,
                    tier,
                    "issued" if status == "confirmed" else rng.choice(TICKET_STATUSES),
                )
            )
    await conn.copy_records_to_table(
        "bookings",
        records=bookings,
        columns=["uid", "amount", "status", "event_uid", "expires_at"],
    )
    print(f"bookings       {len(bookings):>8}")
    await conn.copy_records_to_table(
        "tickets",
        records=tickets,
        columns=["uid", "event_uid", "booking_uid", "ticket_tier_uid", "status"],
    )
    print(f"tickets        {len(tickets):>8}")

    if args.indexes:
        await apply_indexes(conn, create=True)

    # the planner is only as good as its stats, and a bulk load leaves none
    await conn.execute("; ".join(f"analyze {t}" for t in TABLES))

    probe_uid = await conn.fetchval("select uid from users where email=$1", PROBE_EMAIL)
    probe_count = await conn.fetchval(
        "select count(*) from memberships where user_uid=$1", probe_uid
    )
    print(f"\ndone in {perf_counter() - started:.1f}s")
    print(f"probe user {PROBE_EMAIL} -> {probe_uid}  ({probe_count} orgs)")
    print(f"  cookie: {Config.auth_cookie_name}={get_token(probe_uid)}")
    print("\ntry:")
    print("  explain analyze select o.* from organisations o")
    print("  join memberships m on m.org_uid = o.uid")
    print(f"  where m.user_uid = '{probe_uid}' order by o.id limit 10;")
    print(f"\n  GET /events?longitude={args.lon}&latitude={args.lat}&radius=10&limit=10")
    print("  (run with and without --indexes to compare the plans)")

    await conn.close()


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--users", type=int, default=10_000)
    p.add_argument("--orgs", type=int, default=10_000)
    p.add_argument(
        "--extra-members",
        type=int,
        default=10_000,
        help="member rows on top of the one owner per org",
    )
    p.add_argument(
        "--probe-orgs",
        type=int,
        default=200,
        help="how many orgs probe@seed.local belongs to",
    )
    p.add_argument("--venues", type=int, default=10_000)
    p.add_argument("--events", type=int, default=40_000)
    p.add_argument(
        "--ticketed-events",
        type=int,
        default=1_000,
        help="how many events get ticket tiers",
    )
    p.add_argument("--bookings", type=int, default=20_000)
    p.add_argument(
        "--cluster-share",
        type=float,
        default=0.1,
        help="fraction of venues placed near the anchor (default 0.1)",
    )
    p.add_argument("--lon", type=float, default=ANCHOR_LON, help="proximity anchor")
    p.add_argument("--lat", type=float, default=ANCHOR_LAT, help="proximity anchor")
    p.add_argument("--keep", action="store_true", help="append instead of truncating")
    p.add_argument("--seed", type=int, default=0, help="rng seed, for repeatable data")
    p.add_argument(
        "--indexes", action="store_true", help="create the geo/join indexes after seeding"
    )
    p.add_argument(
        "--drop-indexes", action="store_true", help="drop those indexes and exit"
    )
    asyncio.run(seed(p.parse_args()))


if __name__ == "__main__":
    main()
