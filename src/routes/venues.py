from fastapi import APIRouter, HTTPException, status, Query
from typing import Annotated
from uuid import UUID, uuid4
from asyncpg import Record
from pydantic import BaseModel

from ..auth.deps import CurrentUser
from ..database.db import DBSession
from ..database.models import Base, Point, MemberRole

router = APIRouter()


class VenueCreateRequest(BaseModel):
    org_uid: UUID
    name: str
    location: Point


class VenueResponse(Base):
    name: str
    location: Point


class VenueListResponse(BaseModel):
    venues: list[VenueResponse]


VENUE_COLUMNS = (
    "id, uid, created_at, updated_at, org_uid, name, " "ST_AsText(location) as location"
)


async def _get_role(db: DBSession, org_uid: UUID, user_uid: UUID) -> str | None:
    row: Record = await db.fetchrow(
        "select role from memberships where org_uid=$1 and user_uid=$2",
        org_uid,
        user_uid,
    )
    return row.get("role") if row else None


@router.post("/venues", response_model=VenueResponse)
async def create_venue(venue: VenueCreateRequest, db: DBSession, user: CurrentUser):
    if await _get_role(db, venue.org_uid, user.uid) != MemberRole.OWNER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Not a owner. Owner can only remove members"
        )

    row: Record = await db.fetchrow(
        f"""
                    insert into venues(uid, org_uid, creator_user_uid, name, location)
                        values ($1, $2, $3, $4, $5)
                    returning {VENUE_COLUMNS}
            """,
        uuid4(),
        venue.org_uid,
        user.uid,
        venue.name,
        venue.location.to_wkt(),
    )

    return VenueResponse(**row)


@router.get("/venues", response_model=VenueListResponse)
async def list_venues(
    db: DBSession, after: int = 0, limit: Annotated[int, Query(ge=1, le=100)] = 10
):
    rows: list[Record] = await db.fetch(
        f"""select {VENUE_COLUMNS} from venues where id > $1 order by id asc limit $2""",
        after,
        limit,
    )
    data = VenueListResponse(venues=[VenueResponse(**venue) for venue in rows])
    return data


@router.get("/venues/{uid}", response_model=VenueResponse)
async def get_venues(uid: UUID, db: DBSession):
    row: Record = await db.fetchrow(
        f"""select {VENUE_COLUMNS} from venues where uid=$1 limit 1""", uid
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return VenueResponse(**row)
