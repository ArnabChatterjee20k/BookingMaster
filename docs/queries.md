# Query shapes in the booking path

Working notes on the SQL behind `POST /events/{uid}/bookings`. Every claim here
was run against the project's own Postgres (16.15, the compose stack) rather than
reasoned about — the failures documented below are ones that actually reproduced,
and a couple of them contradicted the first guess.

The booking path is the only place in the codebase that takes row locks under
contention, so it is worth writing down *why* each shape was chosen and what the
alternatives cost.

---

## 1. The four shapes and what each is for

| shape | where | the problem it solves |
|---|---|---|
| `= any($2::text[])` | tier + price lookup | validate names and fetch prices in one trip |
| `cross join lateral` | reserve query | a **per-row `LIMIT`** — a different count per tier |
| chained CTEs (`with a, b`) | replenish query | compute a batch size *before* the update reads it |
| data-modifying CTE | replenish query | `update` + `insert` atomically in one statement |

The rule that decides between them: **lateral is for a correlated `LIMIT`.**
Everything else — per-row scalar expressions, joins, filters — a plain join
already does. Reaching for lateral when the limit is constant buys nothing.

---

## 2. Set difference, and why it is gone

The tier-name validation originally read:

```sql
select unnest($2::text[]) as tier
except
select name from tickets_tier where event_uid = $1
```

`except` is the right tool for "which of these names are bogus" — it is a set
difference, it dedupes for free, and it returns only the offenders. It was
removed anyway, because it answers *only* that question. The booking's `amount`
needs `price`, so keeping `except` meant a second round trip to the same rows.

The replacement pulls both and does the difference in Python:

```sql
select name, price
from tickets_tier
where event_uid = $1 and name = any($2::text[])
```

```python
tier_prices = {tier["name"]: tier["price"] for tier in tiers}
missing_tiers = [tier for tier in ticket_tiers if tier not in tier_prices]
```

Measured plan:

```
Index Scan using tickets_tier_event_uid_name_idx on tickets_tier (actual rows=2)
  Index Cond: (event_uid = '...'::uuid)
  Filter: ((name)::text = ANY ('{GENERAL,VIP}'::text[]))
```

Note what the plan actually does: it seeks on `event_uid` and **filters** the
names inside that range — it is not a seek per name. That is fine here (an event
has a handful of tiers, so the range is tiny), but it is the reason this does not
scale to "find these 10,000 names across all events". Different query, different
shape.

Iterating `ticket_tiers` rather than the result set keeps the error message in
the order the client sent, instead of index order.

**`price` arrives as `decimal.Decimal`**, because the column is
`numeric(12, 2)`. Do not let it become a float on the way to `bookings.amount`.

---

## 3. Correlated `LIMIT`: `cross join lateral`

Each tier needs a *different* number of rows locked. A plain join cannot express
that — `LIMIT` applies to the whole result, not per group. `lateral` lets the
subquery reference the current row of the left side, so the limit becomes data:

```sql
with current_tiers as (
    select * from unnest($1::text[], $2::int[]) as x(tier, qty)
)
select result.id, result.ticket_tier_name
from current_tiers
cross join lateral (
    select t.id, t.ticket_tier_name
    from tickets t
    where t.event_uid = $3
      and t.ticket_tier_name = current_tiers.tier
      and t.status = $4
    order by t.id
    limit current_tiers.qty
    for update skip locked
) result
```

Served by `tickets_event_uid_tier_status`.

### `limit` is a reserved word

`as x(tier, limit)` is a syntax error. Use `qty`, or quote it as `"limit"`.
Same trap in every CTE below.

### The `union all` alternative — and the restriction that decides it

One subquery per tier, unioned, is a real alternative. But **the locking clause
cannot sit on a set-operation arm**, even parenthesised:

```sql
(select ... limit 2 for update skip locked)
union all
(select ... limit 3 for update skip locked)
-- ERROR: FOR UPDATE is not allowed with UNION/INTERSECT/EXCEPT
```

Push each one down a query level and it is accepted:

```sql
select * from (select ... limit 2 for update skip locked) a
union all
select * from (select ... limit 3 for update skip locked) b   -- OK
```

Both return identical rows to the lateral. **The lateral is still preferred**,
because the union needs one branch per tier: the SQL text is built at runtime and
changes with the tier count, so there is no plan-cache reuse, and you are
concatenating SQL strings in a path that takes row locks. The lateral is one
static statement with four parameters no matter how many tiers, and the per-tier
limit travels as data inside the `int[]`.

### `skip locked` skips *other* transactions, not your own

Measured across two real connections:

```
conn1 got [1, 2], conn2 got [3, 4]     -> disjoint
conn1 re-run in same txn got [1, 2]    -> own locks are NOT skipped
```

This is why duplicate tier names in one request are a correctness bug and not
just waste: `[{"A", 2}, {"A", 3}]` makes `unnest` emit `A` twice, the lateral
runs twice, and the second pass hands back **the same rows**. The count says 5
reserved; only 2 rows exist. Deduplicate by tier in a Pydantic validator before
the list ever reaches SQL.

---

## 4. Chaining CTEs

`with` appears **once**; further CTEs are comma-separated.

```sql
with a as (...), b as (...), c as (...)   -- correct
with a as (...) with b as (...)           -- syntax error
```

Worth knowing: since PG12 a non-recursive CTE referenced once is *inlined*, so
`current_tiers` costs nothing and does not act as an optimisation fence. If you
ever need it to be a fence, say `materialized` explicitly.

---

## 5. Data-modifying CTE: `update` + `insert` in one statement

The replenish path draws stock from `tickets_tier.available` and materialises the
rows in `tickets`. Both must happen together or the invariant
`capacity - available == count(tickets)` breaks.

```sql
with current_tiers as (
    select * from unnest($1::text[], $2::int[]) as x(tier, qty)
),
target as (
    select tt.id, tt.name, least(ct.qty, tt.available) as batch
      from tickets_tier tt
      join current_tiers ct on ct.tier = tt.name
     where tt.event_uid = $3
     order by tt.id
       for update of tt
),
bumped as (
    update tickets_tier tt
       set available = tt.available - target.batch,
           updated_at = now()
      from target
     where tt.id = target.id and target.batch > 0
    returning target.name, target.batch
)
insert into tickets (uid, event_uid, ticket_tier_name, status)
select gen_random_uuid(), $3, bumped.name, $4
  from bumped, generate_series(1, bumped.batch)
returning ticket_tier_name
```

Verified: asking 3 of `A` (capacity 5) and 60 of `B` (capacity 100) materialised
`{'A': 3, 'B': 60}` and left `available` at 2 and 40. Asking a further 10 of `A`
returned 2 — short, correctly — and `capacity - available == materialised` held.

### No lateral here, and why

There is no per-row `LIMIT`. `least(ct.qty, tt.available)` is a scalar expression
evaluated once per joined row, which a plain join already produces. Lateral would
add nothing.

### `returning target.batch`, never `tt.available`

`RETURNING` on an `UPDATE` sees the **new** row. Deriving the batch from the
updated `available` reports 0 on the final batch and silently strands the rest of
the capacity. `target` computes `batch` against the pre-update value, and that is
the value that must flow to the insert. This is the same trap `race.py` documents
around its `reservation` update.

### There *is* a lateral — implicitly, in the insert

`from bumped, generate_series(1, bumped.batch)` references `bumped.batch` from an
earlier `FROM` item. Set-returning functions in `FROM` are implicitly lateral, so
no keyword is needed. That is the row multiplication — one tier row becomes
`batch` ticket rows — not tier iteration.

### Join the update on the primary key, not the tier name

`target` locked specific physical rows. The update must write **those** rows, so
match on the key that identifies them. Any logical key re-resolves the row set at
update time, and the re-resolved set can differ from the locked set.

Measured, with the same tier name `A` present on two different events:

```
join on tt.name:  event=ba2c1e9d name=A available=90   <-- our event
                  event=cc000000 name=A available=90   <-- DIFFERENT event, untouched stock
join on tt.id:    event=ba2c1e9d name=A available=90
```

Plans: `Update on tickets_tier tt (actual rows=2)` for the name join versus
`rows=1` for the id join. The `target` CTE locked **one** row — it is filtered by
`event_uid` — but the name join wrote **two**. The second row was modified
without ever being covered by `FOR UPDATE`, so every concurrency guarantee in the
statement simply did not apply to it.

Adding `and tt.event_uid = $3` does fix the name join, since
`tickets_tier_event_uid_name_idx` is unique on the pair. The PK is still better:
it is a 4-byte compare instead of uuid + varchar, `target` already selects it, and
its correctness does not depend on that index staying `unique`.

### `for update of tt`

Plain `for update` was accepted in testing (the single-reference CTE is inlined),
but keep the `of tt`. It states that only tier rows are being locked, and it will
not quietly start locking something else if the `FROM` clause grows.

### `order by tt.id` is deadlock avoidance

Two concurrent bookings, one wanting `(A, B)` and the other `(B, A)`, can take
tier row locks in opposite order and deadlock. Ordering by a stable key makes
every transaction acquire them in the same sequence.

The advisory locks do not have this problem: `pg_try_advisory_xact_lock` is
non-blocking, so a caller that loses one returns rather than queues.

---

## 6. Quick reference

| symptom | cause |
|---|---|
| `syntax error at or near "limit"` | `limit` is reserved — use `qty` |
| `FOR UPDATE is not allowed with UNION/INTERSECT/EXCEPT` | locking clause on a set-op arm — wrap each in a `FROM` subquery |
| `syntax error at or near "with"` | second `with` — chain with a comma |
| `column "tickets_tier" of relation "tickets_tier" does not exist` | qualified target in `SET` — write `set available = ...` |
| final batch returns 0, capacity stranded | `RETURNING` read the post-update value |
| another event's stock moves | update joined on `name` instead of the PK |
| reserved count exceeds rows that exist | duplicate tier names — `skip locked` does not skip your own locks |
