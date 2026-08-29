from fastapi import APIRouter, HTTPException, status, Query
from datetime import datetime
from typing import Annotated, Self
from uuid import UUID, uuid4
from asyncpg import Record
from pydantic import BaseModel, Field, model_validator

from ..auth.deps import CurrentUser
from ..database.db import DBSession
from ..database.models import Base, Point, MemberRole
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
    search: str = ""
    after: int = 0
    limit: int = Field(10, ge=1, le=100)
    location: Point | None = None
    # in km
    radius: int = Field(1, ge=1, le=100)


class EventListResponse(BaseModel):
    events: list[EventResponseWithVenueInfo]


@router.post("/events", response_model=EventResponse)
async def create_event(db: DBSession, event: EventCreateRequest, user: CurrentUser):
    if await get_role(db, event.org_uid, user.uid) != MemberRole.OWNER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Not a owner. Owner can only create event"
        )
    # insert if venue exists in a single query basically by pulling the v.uid from venues and passing other values as constant directly to the select
    # also we have unique index on the uuid already
    row: Record | None = await db.fetchrow(
        """
        insert into events(uid, name, org_uid, performer_uid, venue_uid, starts_at, ends_at)
        select $1, $2, $3, $4, v.uid, $6, $7
          from venues v
         where v.uid = $5
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
def list_events(db: DBSession, filters: Annotated[EventListRequest, Query()]):
    # support within in my radius from my home location
    query = """select e.*, v.performer_uid as performer_uid, ST_AsText(v.location) as location 
                    from events e join venues v
                    on e.venue_uid=v.uid 
                    where
            """
    where = []
