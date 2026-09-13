# TLDR
With asyncpg, your code borrows and returns the connection itself (async with pool.acquire() in get_db_session). With Redis, the client borrows and returns a connection inside every command, so your code never touches connections.

asyncpg can work the Redis way too. The pool has its own query methods, and each call borrows a connection and returns it right away:
await pool.fetch("select ...")    # borrow → run → return, per query
await pool.execute("update ...")  # may run on a different connection

We don't use those for routes because a request needs one connection for all its queries. The booking transaction and its row locks only work if every statement runs on the same socket. So the difference is a choice based on what the database needs, not something the libraries force:

- Postgres: statements depend on the connection they run on (transactions, locks), so we manage the connection per request.
- Redis: commands don't depend on each other, so letting the client handle it per command is safe. The exception is pipeline/WATCH, where you hold one connection yourself, just like asyncpg.


# Connection pooling: asyncpg vs Redis

Both Postgres (asyncpg) and Redis use a connection pool created once at startup
(`src/__init__.py`), but what a request gets from its dependency is different.

## asyncpg: one connection per request

```
request starts → pool.acquire() → conn (a real socket, reserved for this request)
               → every query in this request uses that same conn
request ends   → conn is released back to the pool (still open, not closed)
```

- When the request finishes, the connection is **returned to the pool**, not
  closed. The next request reuses the same open socket.
- No other request can use that connection while this request holds it.

See `get_db_session` in `src/database/db.py`.

## Redis: one client for the app, one connection per command

```
app startup   → ONE client created (wraps the pool)
request 1     → cache.get()  → borrow conn → send → reply → return conn
              → cache.set()  → borrow conn → send → reply → return conn
request 2     → cache.get()  → borrow conn → ...        (same client object)
app shutdown  → client.aclose() → pool closes its sockets
```

- Every request gets the **same** client object.
- The client holds no connection itself. **Each command** borrows one just for
  that command and returns it right away.
- So even within one request, `get` and `set` may run on different sockets.
  That's fine because they don't depend on each other.

See `create_cache_client` and `get_cache` in `src/cache/cache.py`.

## Side by side

| | asyncpg | redis |
|---|---|---|
| Object created once at startup | pool | pool + one client |
| What a request gets | its own connection | the shared client |
| How long a connection is held | the whole request | a single command |
| Connections returned to pool | when the request ends | after each command |
| Closed for real | at app shutdown | at app shutdown |

## Why they differ

Postgres transactions and row locks live on one connection, so a request has to
keep the same connection throughout. For example, the booking flow's
`db.transaction()` with `SELECT ... FOR UPDATE` only works because every
statement runs on the same socket.

A Redis command is complete on its own, so any free connection will do. The
exceptions are `pipeline`, `WATCH` and `pubsub`, which hold one connection for
their block, just like asyncpg does for a request:

```python
async with cache.pipeline(transaction=True) as pipe:
    ...
```