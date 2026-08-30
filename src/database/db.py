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
        "create index if not exists events_venue_uid_idex on events(venue_uid);"
    )

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
            available integer not null
        )
    """)

    # bookings
    await conn.execute("""
        create table if not exists bookings (
            id serial primary key,
            uid uuid unique not null,
            created_at timestamptz default now(),
            updated_at timestamptz default now(),
            amount numeric(12, 2) default 0.00,
            status varchar(16) not null,
            event_uid uuid not null,
            expires_at timestamptz not null
        )
    """)

    # tickets
    await conn.execute("""
        create table if not exists tickets (
            id serial primary key,
            uid uuid unique not null,
            created_at timestamptz default now(),
            updated_at timestamptz default now(),
            event_uid uuid not null,
            booking_uid uuid not null,
            ticket_tier_uid uuid not null,
            status varchar(16) not null
        )
    """)

    await conn.close()
