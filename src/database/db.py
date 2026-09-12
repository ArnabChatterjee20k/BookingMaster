import asyncpg
from fastapi import Depends
from typing import Annotated
from ..config import Config


async def get_db():
    conn: asyncpg.Connection = await asyncpg.connect(Config.db_uri)
    return conn


async def get_db_session():
    conn: asyncpg.Connection = await asyncpg.connect(Config.db_uri)
    try:
        yield conn
    finally:
        await conn.close()


DBSession = Annotated[asyncpg.Connection, Depends(get_db_session)]


async def load_schemas():
    conn = await get_db()
    # postgis extension
    await conn.execute("create extension if not exists postgis")

    # string collation for case insensitive matching
    await conn.execute(
        "CREATE COLLATION IF NOT EXISTS utf8_ci_ai ( provider = icu, locale = 'und-u-ks-level1', deterministic = false )"
    )

    # users
    await conn.execute("""
        create table if not exists users (
            id serial primary key,
            uid uuid unique not null,
            created_at timestamptz default now(),
            updated_at timestamptz default now(),
            email varchar(255) unique,
            name varchar(64) not null,
            password varchar(64) not null
        )
    """)

    # organisations
    await conn.execute("""
        create table if not exists organisations (
            id serial primary key,
            uid uuid unique not null,
            created_at timestamptz default now(),
            updated_at timestamptz default now(),
            name varchar(64) not null
        )
    """)

    # memberships
    await conn.execute("""
        create table if not exists memberships (
            id serial primary key,
            uid uuid unique not null,
            created_at timestamptz default now(),
            updated_at timestamptz default now(),
            org_uid uuid not null,
            user_uid uuid not null,
            role varchar(16) not null,
            unique(org_uid, user_uid)
        )
    """)

    await conn.execute(
        "create index if not exists memberships_user_uid_id_idx on memberships(user_uid, id);"
    )

    # venues
    await conn.execute("""
        create table if not exists venues (
            id serial primary key,
            uid uuid unique not null,
            created_at timestamptz default now(),
            updated_at timestamptz default now(),
            creator_user_uid uuid not null,
            org_uid uuid not null,
            name varchar(64) not null,
            location geography(point, 4326) not null,
            unique(name, location)
        )
    """)
    await conn.execute(
        "create index if not exists venues_location_gist on venues USING GIST (location);"
    )

    # events
    await conn.execute("""
        create table if not exists events (
            id serial primary key,
            uid uuid unique not null,
            created_at timestamptz default now(),
            updated_at timestamptz default now(),
            name varchar(64) not null,
            org_uid uuid not null,
            performer_uid uuid not null,
            venue_uid uuid not null,
            starts_at timestamptz not null,
            ends_at timestamptz not null
        )
    """)
    await conn.execute(
        "create index if not exists events_venue_uid_idx on events(venue_uid);"
    )
    # no index is needed on starts_at as we are orderin by id and postgres not using the index on starts_at or (id, starts_at)
    # if ordering by starts_at, id then having index on (starts_at, id) will help

    # tickets tier
    await conn.execute("""
        create table if not exists tickets_tier (
            id serial primary key,
            uid uuid unique not null,
            created_at timestamptz default now(),
            updated_at timestamptz default now(),
            name varchar(64) not null,
            event_uid uuid not null,
            price numeric(12, 2) default 0.00,
            capacity integer not null,
            available integer not null
        )
    """)
    await conn.execute(
        "create unique index if not exists tickets_tier_event_uid_name_idx"
        " on tickets_tier(event_uid, name);"
    )

    # bookings
    await conn.execute("""
        create table if not exists bookings (
            id serial primary key,
            uid uuid not null,
            created_at timestamptz default now(),
            updated_at timestamptz default now(),
            amount numeric(12, 2) default 0.00,
            status varchar(16) not null,
            event_uid uuid not null,
            user_uid uuid not null,
            expires_at timestamptz not null,
            unique(uid, event_uid, user_uid)
        )
    """)
    # serves "list my bookings for this event"; lookups by booking uid already
    # have bookings_uid_key from the unique constraint
    await conn.execute(
        "create index if not exists bookings_event_uid_user_uid_idx"
        " on bookings(event_uid, user_uid, id);"
    )
    # serves "list all my bookings"; the event-leading index above cannot, since
    # a btree is only seekable from its leading column
    await conn.execute(
        "create index if not exists bookings_user_uid_id_idx"
        " on bookings(user_uid, id);"
    )

    # tickets
    # not referencing ticket_tier_uid as the ticket_tier is rarely going to change
    # and having the name can save lookups a lot to other tables
    # not doing for events as the details might change
    await conn.execute("""
        create table if not exists tickets (
            id serial primary key,
            uid uuid unique not null,
            created_at timestamptz default now(),
            updated_at timestamptz default now(),
            event_uid uuid not null,
            booking_uid uuid,
            ticket_tier_name varchar(64) not null,
            status varchar(16) not null
        )
    """)

    await conn.execute(
        "create index if not exists tickets_event_uid_tier_status"
        " on tickets(event_uid, ticket_tier_name, status);"
    )

    # the booking reads group tickets by booking_uid; without this every read
    # is a seq scan of the whole tickets table
    await conn.execute(
        "create index if not exists tickets_booking_uid_idx"
        " on tickets(booking_uid) where booking_uid is not null;"
    )

    await conn.close()
