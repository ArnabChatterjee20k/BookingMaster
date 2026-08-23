"""Seed the dev database with enough rows to make index work measurable.

    python seed.py                      # 10k users, 10k orgs, ~20k memberships
    python seed.py --users 50000 --orgs 50000 --extra-members 100000
    python seed.py --keep               # append instead of truncating first

Every organisation gets exactly one owner, then `--extra-members` additional
(org, user) pairs are drawn at random, so the membership table has realistic
fan-out rather than a flat 1:1 mapping.

One user -- probe@seed.local -- is deliberately placed in `--probe-orgs`
organisations so `list_organisations` has something to paginate through. Its
uid is printed at the end; use it in EXPLAIN ANALYZE.

Rows are loaded with COPY (asyncpg's copy_records_to_table), which is the
fastest bulk path -- far quicker than executemany for this volume.
"""

import argparse
import asyncio
import random
import uuid
from time import perf_counter

import asyncpg

from src.config import Config
from src.database.db import load_schemas

PASSWORD = "seeded-not-a-real-password"
PROBE_EMAIL = "probe@seed.local"
TABLES = ["memberships", "organisations", "users"]


async def seed(args):
    rng = random.Random(args.seed)
    started = perf_counter()

    await load_schemas()
    conn = await asyncpg.connect(Config.db_uri)

    if not args.keep:
        await conn.execute(f"truncate {', '.join(TABLES)} restart identity cascade")
        print(f"truncated {', '.join(TABLES)}")

    # ---------------------------------------------------------------- users
    user_uids = [uuid.uuid4() for _ in range(args.users)]
    users = [
        (user_uids[i], PROBE_EMAIL if i == 0 else f"user{i}@seed.local",
         "probe" if i == 0 else f"user{i}", PASSWORD)
        for i in range(args.users)
    ]
    await conn.copy_records_to_table(
        "users", records=users, columns=["uid", "email", "name", "password"]
    )
    print(f"users          {len(users):>8}")

    # -------------------------------------------------------- organisations
    org_uids = [uuid.uuid4() for _ in range(args.orgs)]
    orgs = [(org_uids[i], f"org-{i}") for i in range(args.orgs)]
    await conn.copy_records_to_table("organisations", records=orgs, columns=["uid", "name"])
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

    for org_i in range(args.orgs):                      # one owner per org
        add(org_i, rng.randrange(args.users), "owner")

    probe_target = min(args.probe_orgs, args.orgs)      # a user worth paginating
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
    print(f"memberships    {len(rows):>8}  ({args.orgs} owner, {len(rows) - args.orgs} member)")

    await conn.execute("analyze users; analyze organisations; analyze memberships")

    probe_uid = await conn.fetchval("select uid from users where email=$1", PROBE_EMAIL)
    probe_count = await conn.fetchval(
        "select count(*) from memberships where user_uid=$1", probe_uid
    )
    print(f"\ndone in {perf_counter() - started:.1f}s")
    print(f"probe user {PROBE_EMAIL} -> {probe_uid}  ({probe_count} orgs)")
    print("\ntry:")
    print("  explain analyze select o.* from organisations o")
    print("  join memberships m on m.org_uid = o.uid")
    print(f"  where m.user_uid = '{probe_uid}' order by o.id limit 10;")

    await conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--users", type=int, default=10_000)
    p.add_argument("--orgs", type=int, default=10_000)
    p.add_argument("--extra-members", type=int, default=10_000,
                   help="member rows on top of the one owner per org")
    p.add_argument("--probe-orgs", type=int, default=200,
                   help="how many orgs probe@seed.local belongs to")
    p.add_argument("--keep", action="store_true", help="append instead of truncating")
    p.add_argument("--seed", type=int, default=0, help="rng seed, for repeatable data")
    asyncio.run(seed(p.parse_args()))


if __name__ == "__main__":
    main()
