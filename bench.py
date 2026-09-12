"""Throughput harness. Prints req/s and DB qps per endpoint.

    python bench.py                          # every scenario, defaults
    python bench.py --requests 500 -c 32     # more load
    python bench.py --only read              # skip the write path
    python bench.py --url http://localhost:8000   # against a running server
    python bench.py --pool 0                 # today's connect-per-request

Reads the numbers the scaling roadmap in readme.md is built on, so re-run it
after every change on that list and paste the table into the PR.

Two columns matter beyond req/s:

  q/req   queries issued per request. A list endpoint whose q/req climbs with
          `--requests` has an N+1; it should be flat.
  qps     queries per second reaching Postgres. req/s is what users feel, qps
          is what the database has to survive -- they diverge by q/req.

DB counting works by wrapping the connection handed to each request, so it
counts real statements, not an estimate. It is only available in-process; with
--url the query columns read "-".

Runs against the real database in Config.db_uri and cleans up the rows it made.
Not a correctness test -- test.py covers that. A scenario that errors is
reported in `err` rather than aborting, since a partly-broken endpoint still has
a useful number next to it.
"""

import argparse
import asyncio
import warnings
import statistics
import time
import uuid

import asyncpg
import httpx

# the dev signing key is short; the warning is per-request noise here
warnings.filterwarnings("ignore", message=".*HMAC key.*")

from src import create_api
from src.config import Config
from src.database import db as db_module

BENCH_EVENT = "dddd0000-0000-4000-8000-00000000000b"
TIERS = (("gold", "750.50", 100000), ("silver", "100.25", 100000))

queries = 0


class CountingConnection:
    """Proxies asyncpg.Connection and counts statements. Only the methods the
    routes actually call are counted; everything else passes straight through."""

    __slots__ = ("_conn",)
    _COUNTED = ("fetch", "fetchrow", "fetchval", "execute", "executemany")

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        attr = getattr(self._conn, name)
        if name not in self._COUNTED:
            return attr

        async def counted(*args, **kwargs):
            global queries
            queries += 1
            return await attr(*args, **kwargs)

        return counted


def percentile(values, pct):
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(int(round(pct / 100 * len(ordered) + 0.5)) - 1, len(ordered) - 1)
    return ordered[k]


async def seed(conn, users):
    """One event with two large tiers, a user, and some bookings to read."""
    await teardown(conn)
    # a real venue row: GET /events/{uid} joins venues, so a dangling
    # venue_uid would make that scenario measure a 404 path
    venue_uid = await conn.fetchval(
        """insert into venues(uid, name, creator_user_uid, org_uid, location)
           values(gen_random_uuid(), $1, gen_random_uuid(), gen_random_uuid(),
                  'SRID=4326;POINT(77.5946 12.9716)')
           returning uid""",
        f"bench-venue-{uuid.uuid4().hex[:8]}",
    )
    await conn.execute(
        """insert into events(uid, name, org_uid, performer_uid, venue_uid,
                              starts_at, ends_at)
           values($1, 'bench', gen_random_uuid(), gen_random_uuid(), $2,
                  now() + interval '30 days',
                  now() + interval '30 days 3 hours')""",
        BENCH_EVENT, venue_uid,
    )
    for name, price, capacity in TIERS:
        await conn.execute(
            """insert into tickets_tier(uid, name, event_uid, price, capacity, available)
               values(gen_random_uuid(), $1, $2, $3::numeric, $4::int, $4::int)""",
            name, BENCH_EVENT, price, capacity,
        )
    await conn.execute("analyze tickets_tier")


async def teardown(conn):
    for table in ("tickets", "bookings", "tickets_tier"):
        await conn.execute(f"delete from {table} where event_uid=$1", BENCH_EVENT)
    await conn.execute("delete from events where uid=$1", BENCH_EVENT)
    await conn.execute(
        "delete from users where email like 'bench-%@bench.local'"
    )
    await conn.execute("delete from venues where name like 'bench-venue-%'")


async def signup(client):
    email = f"bench-{uuid.uuid4().hex[:10]}@bench.local"
    r = await client.post(
        "/users", json={"email": email, "name": "bench", "password": "hunter2"}
    )
    r.raise_for_status()
    return {Config.auth_cookie_name: r.cookies.get(Config.auth_cookie_name)}


async def run(name, client, make_request, total, concurrency, counts_db):
    """Fire `total` requests `concurrency` at a time; return one table row."""
    global queries
    latencies, errors = [], 0
    queries = 0
    per_worker = max(total // concurrency, 1)

    async def worker():
        nonlocal errors
        for _ in range(per_worker):
            started = time.perf_counter()
            try:
                response = await make_request(client)
                if response.status_code >= 400:
                    errors += 1
            except Exception:
                errors += 1
            latencies.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*[worker() for _ in range(concurrency)])
    elapsed = time.perf_counter() - started
    done = per_worker * concurrency

    return {
        "name": name,
        "reqs": done,
        "conc": concurrency,
        "rps": done / elapsed,
        "p50": statistics.median(latencies) if latencies else 0.0,
        "p95": percentile(latencies, 95),
        "p99": percentile(latencies, 99),
        "err": errors,
        "queries": queries if counts_db else None,
        "qpr": (queries / done) if counts_db and done else None,
        "qps": (queries / elapsed) if counts_db else None,
    }


def table(rows):
    head = ("scenario", "reqs", "conc", "req/s", "p50 ms", "p95 ms", "p99 ms",
            "err", "q/req", "qps")
    widths = [max(len(head[0]), *(len(r["name"]) for r in rows)), 6, 5, 10, 8, 8, 8, 5, 7, 10]
    line = "  ".join(h.rjust(w) if i else h.ljust(w) for i, (h, w) in enumerate(zip(head, widths)))
    print("\n" + line)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        cells = [
            r["name"].ljust(widths[0]),
            f"{r['reqs']:,}".rjust(widths[1]),
            str(r["conc"]).rjust(widths[2]),
            f"{r['rps']:,.0f}".rjust(widths[3]),
            f"{r['p50']:.2f}".rjust(widths[4]),
            f"{r['p95']:.2f}".rjust(widths[5]),
            f"{r['p99']:.2f}".rjust(widths[6]),
            (str(r["err"]) if r["err"] else "-").rjust(widths[7]),
            (f"{r['qpr']:.1f}" if r["qpr"] is not None else "-").rjust(widths[8]),
            (f"{r['qps']:,.0f}" if r["qps"] is not None else "-").rjust(widths[9]),
        ]
        print("  ".join(cells))


async def main(args):
    admin = await asyncpg.connect(Config.db_uri)
    await seed(admin, 1)

    counts_db = args.url is None
    pool = None

    if args.url:
        client_kwargs = {"base_url": args.url}
    else:
        app = create_api()
        if args.pool:
            pool = await asyncpg.create_pool(
                Config.db_uri, min_size=args.pool, max_size=args.pool
            )

            async def get_session():
                async with pool.acquire() as conn:
                    yield CountingConnection(conn) if counts_db else conn
        else:

            async def get_session():
                conn = await asyncpg.connect(Config.db_uri)
                try:
                    yield CountingConnection(conn) if counts_db else conn
                finally:
                    await conn.close()

        app.dependency_overrides[db_module.get_db_session] = get_session
        client_kwargs = {
            "transport": httpx.ASGITransport(app=app),
            "base_url": "http://bench",
        }

    async with httpx.AsyncClient(timeout=60, **client_kwargs) as client:
        auth = await signup(client)

        seeded = []
        for _ in range(args.requests if args.requests < 20 else 20):
            r = await client.post(
                f"/events/{BENCH_EVENT}/bookings",
                json={"booking_uid": str(uuid.uuid4()),
                      "tickets": [{"tier_name": "gold", "quantity": 1}]},
                cookies=auth,
            )
            if r.status_code < 400:
                seeded.append(r.json()["booking_uid"])
        if not seeded:
            print("  ! could not seed bookings -- read scenarios will be thin")

        read_scenarios = [
            ("health", lambda c: c.get("/health")),
            ("GET /events", lambda c: c.get("/events", params={"limit": 10})),
            ("GET /events/{uid}", lambda c: c.get(f"/events/{BENCH_EVENT}")),
            ("GET tickets/tier", lambda c: c.get(f"/events/{BENCH_EVENT}/tickets/tier")),
            ("GET /bookings", lambda c: c.get("/bookings", params={"limit": 10}, cookies=auth)),
            ("GET /bookings/{uid}", lambda c: c.get(f"/bookings/{seeded[0]}", cookies=auth)
                if seeded else c.get("/health")),
            ("GET event bookings", lambda c: c.get(f"/events/{BENCH_EVENT}/bookings",
                                                   params={"limit": 10}, cookies=auth)),
        ]

        def booking(c):
            return c.post(
                f"/events/{BENCH_EVENT}/bookings",
                json={"booking_uid": str(uuid.uuid4()),
                      "tickets": [{"tier_name": "gold", "quantity": 1}]},
                cookies=auth,
            )

        write_scenarios = [("POST bookings", booking)]

        chosen = []
        if args.only in (None, "read"):
            chosen += read_scenarios
        if args.only in (None, "write"):
            chosen += write_scenarios

        mode = args.url or (f"in-process, pool={args.pool}" if args.pool
                            else "in-process, connect-per-request")
        print(f"\n  {mode}   {args.requests} requests/scenario, concurrency {args.concurrency}")

        rows = []
        for name, make in chosen:
            rows.append(await run(name, client, make, args.requests,
                                  args.concurrency, counts_db))
        table(rows)

        if counts_db:
            print("\n  q/req is queries per request -- flat as --requests grows means no N+1.")
        if any(r["err"] for r in rows):
            print("  err counts 4xx and 5xx; POST bookings 409s when the tier is contended.")

    if pool:
        await pool.close()
    await teardown(admin)
    await admin.close()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--requests", "-n", type=int, default=200,
                   help="requests per scenario (default 200)")
    p.add_argument("--concurrency", "-c", type=int, default=16,
                   help="in-flight requests (default 16)")
    p.add_argument("--pool", type=int, default=20,
                   help="pool size; 0 reproduces today's connect-per-request")
    p.add_argument("--only", choices=("read", "write"),
                   help="restrict to read or write scenarios")
    p.add_argument("--url", help="benchmark a running server instead of in-process")
    raise SystemExit(asyncio.run(main(p.parse_args())))
