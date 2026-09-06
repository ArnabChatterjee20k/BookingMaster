from fastapi import APIRouter, HTTPException, status, Query
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Annotated, Self
from uuid import UUID, uuid4
from asyncpg import Record
from pydantic import BaseModel, Field, model_validator

from ..auth.deps import CurrentUser
from ..database.db import DBSession
from ..database.models import Base, Point, MemberRole
from ..database.query import QueryBuilder
from ..database.utils import get_role

router = APIRouter()


class EventResponse(Base):
    name: str
    org_uid: UUID
    performer_uid: UUID
    venue_uid: UUID
    # utc times
    starts_at: datetime
    ends_at: datetime


class Venue(BaseModel):
    name: str
    location: Point


class EventResponseWithVenueInfo(EventResponse):
    venue: Venue


class EventCreateRequest(BaseModel):
    name: str
    org_uid: UUID
    performer_uid: UUID
    venue_uid: UUID
    # utc times
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def convert_timestamp_timezone_to_utc(self) -> Self:
        if self.starts_at.tzinfo is None:
            raise ValueError("starts_at must have timezone")
        if self.ends_at.tzinfo is None:
            raise ValueError("ends_at must have timezone")
        return self


class EventListRequest(BaseModel):
    after: int = 0
    limit: int = Field(10, ge=1, le=100)
    longitude: float | None = Field(None, ge=-180, le=180)
    latitude: float | None = Field(None, ge=-90, le=90)
    # in km
    radius: int = Field(1, ge=1, le=100, description="Radius in km")

    @model_validator(mode="after")
    def coords_come_in_pairs(self) -> Self:
        if (self.longitude is None) != (self.latitude is None):
            raise ValueError("longitude and latitude must be given together")
        return self


class EventListResponse(BaseModel):
    events: list[EventResponseWithVenueInfo]

class TicketsTierRequest(BaseModel):
    name: str
    price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    capacity: int = Field(ge=0)


class TicketTierResponse(Base):
    name: str
    price: Decimal
    event_uid: UUID
    capacity: int
    available: int

@router.post("/events", response_model=EventResponse)
async def create_event(db: DBSession, event: EventCreateRequest, user: CurrentUser):
    if await get_role(db, event.org_uid, user.uid) != MemberRole.OWNER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Not a owner. Owner can only create event"
        )
    async with db.transaction():
        venue: Record | None = await db.fetchrow("""select 1 from venues where uid=$1 and org_uid=$2 for update""", event.venue_uid, event.org_uid)
        if not venue:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "venue not found")

        # check any events scheduled on a venue id or not. atleast 2days gap
        row: Record | None = await db.fetchrow(
                    """
                    select id
                    from events
                    where venue_uid = $1
                    and starts_at BETWEEN $2::timestamptz - interval '2 days'
                                        AND $2::timestamptz + interval '2 days'
                    limit 1
                    """,
                    event.venue_uid,
                    event.starts_at,
                )
        if row:
            raise HTTPException(status.HTTP_409_CONFLICT, "venue already booked and should have a gap of atleast of 2days before start and after start")

        row: Record | None = await db.fetchrow(
            """
            insert into events(uid, name, org_uid, performer_uid, venue_uid, starts_at, ends_at)
            values ($1, $2, $3, $4, $5, $6, $7)
            returning *
        """,
            uuid4(),
            event.name,
            event.org_uid,
            event.performer_uid,
            event.venue_uid,
            event.starts_at,
            event.ends_at,
        )

    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "venue not found")
    return EventResponse(**row)


@router.get("/events/{uid}", response_model=EventResponseWithVenueInfo)
async def get_event(db: DBSession, uid: UUID):
    query = """select e.*,
                      v.name as venue_name,
                      ST_AsText(v.location) as venue_location
                 from events e
                 join venues v on v.uid = e.venue_uid
                where e.uid = $1"""
    row: Record | None = await db.fetchrow(query, uid)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    return EventResponseWithVenueInfo(
        **row,
        venue=Venue(name=row["venue_name"], location=row["venue_location"]),
    )


@router.get("/events", response_model=EventListResponse)
async def list_events(db: DBSession, filters: Annotated[EventListRequest, Query()]):
    q = QueryBuilder()

    q.where("e.id > {}", filters.after)
    # events starting before one day
    q.where("e.starts_at >= {}", datetime.now(timezone.utc) + timedelta(days=1))

    if filters.longitude is not None and filters.latitude is not None:
        q.where(
            """
            ST_DWithin(
                v.location,
                ST_SetSRID(ST_MakePoint({}, {}), 4326)::geography,
                {}
            )
            """,
            filters.longitude,
            filters.latitude,
            filters.radius * 1000,  # converting km to m as pg will be using the m
        )

    q.args.append(filters.limit)

    query = f"""
            select
                e.*,
                v.name as venue_name,
                ST_AsText(v.location) as venue_location
            from events e
            join venues v ON e.venue_uid = v.uid
            where {q.build_where()}
            order by e.id ASC
            limit ${len(q.args)}
        """

    rows: list[Record] = await db.fetch(query, *q.args)
    return EventListResponse(
        events=[
            EventResponseWithVenueInfo(
                **row,
                venue=Venue(name=row["venue_name"], location=row["venue_location"]),
            )
            for row in rows
        ]
    )

# upsert
@router.put("/events/{uid}/tickets/tier", response_model=TicketTierResponse)
async def create_ticket_tiers(uid: UUID, ticket_tier:TicketsTierRequest, db: DBSession, user: CurrentUser):
    event: Record = await db.fetchrow("select org_uid, starts_at, ends_at from events where uid=$1 limit 1", uid)
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")
    
    if await get_role(db, event.get("org_uid"), user.uid) != MemberRole.OWNER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Not a owner. Owner can only create tickets tier"
        )

    async with db.transaction():
        existing: Record | None = await db.fetchrow(
            "select capacity, available from tickets_tier"
            " where event_uid=$1 and name=$2 for update",
            uid,
            ticket_tier.name,
        )
        if existing:
            materialised = existing["capacity"] - existing["available"]
            if ticket_tier.capacity < materialised:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"{materialised} tickets already exist for this tier;"
                    f" capacity cannot go below that",
                )

        # unique index on (event_uid, name)
        ticket_tier: Record = await db.fetchrow(
                                """
                                insert INTO tickets_tier
                                    (uid, name, event_uid, price, capacity, available)
                                values
                                    ($1, $2, $3, $4, $5, $5)
                                on conflict (event_uid, name)
                                do update set
                                    price = excluded.price,
                                    capacity = excluded.capacity,
                                    -- every right-hand side reads the OLD row, so
                                    -- this is new_capacity minus what is already
                                    -- materialised. Rows in the pool are untouched;
                                    -- only the un-materialised remainder moves.
                                    available = excluded.capacity
                                                - (tickets_tier.capacity - tickets_tier.available),
                                    updated_at = now()
                                returning *
                                """,
                                uuid4(),
                                ticket_tier.name,
                                uid,
                                ticket_tier.price,
                                ticket_tier.capacity,
                            )

    return TicketTierResponse(**ticket_tier)

@router.get("/events/{uid}/tickets/tier", response_model=TicketTierResponse)
async def get_ticket_tiers(uid: UUID, db: DBSession):
    tier: Record | None = await db.fetchrow("select * from tickets_tier where event_uid=$1", uid)
    if not tier:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return TicketTierResponse(**tier)

# not adding the delete ticket tier for now as its not going to get used a lot