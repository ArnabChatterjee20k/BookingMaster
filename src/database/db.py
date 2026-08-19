import asyncpg
from ..config import Config

async def get_db():
    conn: asyncpg.Connection = await asyncpg.connect(Config.db_uri)
    return conn

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
            email varchar(255) unique
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
            org_id integer not null,
            user_id integer not null,
            role varchar(16) not null,
            unique(org_id, user_id)
        )
    """)

    # venues
    await conn.execute("""
        create table if not exists venues (
            id serial primary key,
            uid uuid unique not null,
            created_at timestamptz default now(),
            updated_at timestamptz default now(),
            name varchar(64) not null,
            location geography(point, 4326) not null
        )
    """)

    # events
    await conn.execute("""
        create table if not exists events (
            id serial primary key,
            uid uuid unique not null,
            created_at timestamptz default now(),
            updated_at timestamptz default now(),
            name varchar(64) not null,
            org_id integer not null,
            performer_id integer not null,
            venue_id integer not null,
            starts_at timestamptz not null,
            ends_at timestamptz not null
        )
    """)

    # tickets tier
    await conn.execute("""
        create table if not exists tickets_tier (
            id serial primary key,
            uid uuid unique not null,
            created_at timestamptz default now(),
            updated_at timestamptz default now(),
            name varchar(64) not null,
            event_id integer not null,
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
            event_id integer not null,
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
            event_id integer not null,
            booking_id integer not null,
            ticket_tier_id integer not null,
            status varchar(16) not null
        )
    """)

    await conn.close()
