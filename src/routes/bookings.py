from fastapi import APIRouter, HTTPException, Query, status
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import UUID
from typing import Self
from asyncpg import Record
from pydantic import BaseModel, Field, model_validator

from ..auth.deps import CurrentUser
from ..database.db import DBSession
from ..database.models import Base, BookingStatus, TicketStatus
from ..database.query import QueryBuilder
from ..database.utils import get_role

router = APIRouter()


class Ticket(BaseModel):
    tier_name: str
    quantity: int = Field(gt=0)


class ReservedTicket(Ticket):
    id: int


class BookingCreateRequest(BaseModel):
    tickets: list[Ticket]
    booking_uid: UUID

    @model_validator(mode="after")
    def dedup_ticket_tiers(self) -> Self:
        merged: dict[str, int] = {}
        for ticket in self.tickets:
            merged[ticket.tier_name] = merged.get(ticket.tier_name, 0) + ticket.quantity
        self.tickets = [Ticket(tier_name=n, quantity=q) for n, q in merged.items()]
        return self


class BookingResponse(BaseModel):
    tickets: list[Ticket]
    booking_uid: UUID
    amount: Decimal


class BookingDetail(BookingResponse):
    event_uid: UUID
    status: BookingStatus
    created_at: datetime
    expires_at: datetime


class BookingListResponse(BaseModel):
    bookings: list[BookingDetail]
    next: int | None = None


POOL_SIZE = 100
MAX_CURSOR = 2**31 - 1

# using lateral query to iterate over tier columns which will help to reference each tier from the cte and apply limit for each
# each tier matching with each tier via the cross join
# basically this flow current tiers like (A,B,C) cross join (tickets table where tier is A/B/C and limit for accordingly)
# we could have used unionall as well but pg allows for update with select only so select * from (select + update) unionall select * from (select + update)
reserve_query = """
            with current_tiers as (
                select * from unnest($1::text[], $2::int[]) as x(tier, qty)
            )
            select result.id , result.ticket_tier_name from
            current_tiers cross join lateral (
                select ticket.id, ticket.ticket_tier_name from
                tickets ticket where
                    ticket.event_uid=$3 and
                    ticket.ticket_tier_name = current_tiers.tier and
                    ticket.status=$4
                order by ticket.id
                limit current_tiers.qty
                for update skip locked
            ) result
        """

# no lateral as the limit is constant
# join on tiers and ticket_tiers on tier-name for update -> update available -> insert batch
replinish_query = """
    with current_tiers as (
        select tier, $2::int as qty from unnest($1::text[]) as tier
    ),
    target as (
        select tickets_tier.id, tickets_tier.name,
                greatest(0, least(current_tiers.qty, tickets_tier.available)) as batch
        from tickets_tier join current_tiers on tickets_tier.event_uid = $3 and tickets_tier.name = current_tiers.tier
        for update of tickets_tier
    ),
    bumped as (
        update tickets_tier set
            available = tickets_tier.available - target.batch,
            updated_at = now()
        from target
        where tickets_tier.id = target.id and target.batch > 0
        returning target.name, target.batch
    )
    insert into tickets (uid, event_uid, ticket_tier_name, status)
    select gen_random_uuid(), $3, bumped.name, $4
    from bumped, generate_series(1, bumped.batch)
    returning ticket_tier_name
"""

# we could use the unionall as well SELECT key FROM (SELECT 12 AS key, pg_try_advisory_xact_lock(12) AS acquired UNION ALL SELECT 14, pg_try_advisory_xact_lock(14) ) WHERE NOT acquired;
take_lock_query = """
    SELECT lock_key
    FROM unnest($1::text[]) AS x(lock_key)
    WHERE NOT pg_try_advisory_xact_lock(
        hashtextextended(lock_key, 0)
    )
"""

claim_query = """
    update tickets set booking_uid=$1, status=$2, updated_at=now()
    where id = any($3::int[])
"""

booking_columns = """
    id, uid, created_at, updated_at, event_uid, user_uid, amount, status, expires_at
"""

get_booking_query = f"""
    select {booking_columns}
    from bookings
    where uid = $1 and user_uid = $2
"""

list_bookings_query = f"""
    select {booking_columns}
    from bookings
    where user_uid = $1 and id < $2
    order by id desc
    limit $3
"""

list_bookings_of_event_query = f"""
    select {booking_columns}
    from bookings
    where event_uid = $1 and user_uid = $2 and id < $3
    order by id desc
    limit $4
"""

# one round trip for every booking on the page instead of one per booking
booking_tickets_query = """
    select booking_uid, ticket_tier_name, count(*) as quantity
    from tickets
    where booking_uid = any($1::uuid[])
    group by booking_uid, ticket_tier_name
"""

book_query = """
    insert into bookings(uid, event_uid, user_uid, amount, status, expires_at)
    values($1, $2, $3, $4, $5, $6)
    on conflict (uid) do update
       set status=excluded.status,
           expires_at=excluded.expires_at,
           amount=excluded.amount,
           updated_at=now()
     where bookings.user_uid = excluded.user_uid
       and bookings.event_uid = excluded.event_uid
    returning uid
"""


async def _reserve_tickets(db: DBSession, ticket_tiers, ticket_tiers_limit, uid):
    tickets_required_quantity = dict(zip(ticket_tiers, ticket_tiers_limit))
    tickets_slot_available = {tier: 0 for tier in ticket_tiers}

    result = {
        "ticket_reserved_ids": {tier: [] for tier in ticket_tiers},
        "slot_required": [],
    }
    tickets_slot: list[Record] = await db.fetch(
        reserve_query, ticket_tiers, ticket_tiers_limit, uid, TicketStatus.AVAILABLE
    )
    for ticket in tickets_slot:
        tickets_slot_available[ticket.get("ticket_tier_name")] += 1
        result["ticket_reserved_ids"][ticket.get("ticket_tier_name")].append(
            ticket.get("id")
        )

    for tier, reserved in tickets_slot_available.items():
        # will be left with only tiers whose tickets are less
        if reserved >= tickets_required_quantity[tier]:
            continue
        result["slot_required"].append(
            Ticket(quantity=tickets_required_quantity[tier] - reserved, tier_name=tier)
        )

    return result


async def _replinish_tickets(db: DBSession, ticket_tiers, uid):
    await db.execute(
        replinish_query, ticket_tiers, POOL_SIZE, uid, TicketStatus.AVAILABLE
    )


async def _take_lock(db: DBSession, lock_keys) -> list[Record]:
    return await db.fetch(take_lock_query, lock_keys)


async def _booked_response(db: DBSession, booking_uid: UUID, amount) -> BookingResponse:
    rows: list[Record] = await db.fetch(
        """select ticket_tier_name, count(*) as quantity
           from tickets where booking_uid=$1
           group by ticket_tier_name order by ticket_tier_name""",
        booking_uid,
    )
    return BookingResponse(
        tickets=[
            Ticket(tier_name=row["ticket_tier_name"], quantity=row["quantity"])
            for row in rows
        ],
        booking_uid=booking_uid,
        amount=amount,
    )


async def _rebook(
    db: DBSession,
    booking: BookingCreateRequest,
    uid: UUID,
    user,
    tier_prices,
    reserved_ids,
) -> BookingResponse:
    ticket_ids = [ticket_id for ids in reserved_ids.values() for ticket_id in ids]
    await db.execute(claim_query, booking.booking_uid, TicketStatus.BOOKED, ticket_ids)

    total_amount = sum(
        (tier_prices[ticket.tier_name] * ticket.quantity for ticket in booking.tickets),
        Decimal("0.00"),
    )

    # HACK: generally we would want to call the payments api here after this record creation for 10mins. Then mark it as success/failed in other route after payment
    # but since no payment so directly booking it
    # await db.execute(
    #     """insert into bookings(uid, event_uid, user_uid, amount, status, expires_at)
    #        values($1, $2, $3, $4, $5, $6)""",
    #     booking.booking_uid,
    #     uid,
    #     user.uid,
    #     total_amount,
    #     BookingStatus.PENDING,
    #     datetime.now(timezone.utc) + timedelta(minutes=10),
    # )

    issued: Record | None = await db.fetchrow(
        book_query,
        booking.booking_uid,
        uid,
        user.uid,
        total_amount,
        BookingStatus.CONFIRMED,
        datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    if issued is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Already a booking issued in this booking id"
        )

    return BookingResponse(
        tickets=booking.tickets, booking_uid=booking.booking_uid, amount=total_amount
    )


@router.post("/events/{uid}/bookings", response_model=BookingResponse)
async def create_booking(
    uid: UUID, booking: BookingCreateRequest, db: DBSession, user: CurrentUser
):
    event: Record | None = await db.fetchrow(
        """select 1 from events where uid=$1 limit 1""", uid
    )
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")

    ticket_tiers = list(map(lambda b: b.tier_name, booking.tickets))

    tiers: list[Record] = await db.fetch(
        """select name, price
           from tickets_tier
           where event_uid = $1 and name = any($2::text[])""",
        uid,
        ticket_tiers,
    )

    tier_prices: dict[str, Decimal] = {tier["name"]: tier["price"] for tier in tiers}

    # preserves the order the client sent, so the message names them predictably
    missing_tiers = [tier for tier in ticket_tiers if tier not in tier_prices]
    if missing_tiers:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"{', '.join(missing_tiers)} doesn't exist"
        )

    ticket_tiers_limit = list(map(lambda b: b.quantity, booking.tickets))
    async with db.transaction():
        booking_issued: Record | None = await db.fetchrow(
            "select event_uid, user_uid, amount from bookings where uid=$1 limit 1 for update",
            booking.booking_uid,
        )
        if booking_issued and (
            booking_issued.get("event_uid") != uid
            or booking_issued.get("user_uid") != user.uid
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Already a booking issued in this booking id"
            )

        if booking_issued:
            return await _booked_response(
                db, booking.booking_uid, booking_issued.get("amount")
            )

        tickets_required_quantity = dict(zip(ticket_tiers, ticket_tiers_limit))
        reservation_result = await _reserve_tickets(
            db, ticket_tiers, ticket_tiers_limit, uid
        )
        reserved_ids = reservation_result["ticket_reserved_ids"]

        # HACK: generally we would want to call the payments api here after this record creation for 10mins. Then mark it as success/failed in other route after payment
        # but since no payment so directly booking it
        if not reservation_result["slot_required"]:
            return await _rebook(db, booking, uid, user, tier_prices, reserved_ids)

        # inline reserve more for the tier
        slot_tiers = [
            ticket.tier_name for ticket in reservation_result["slot_required"]
        ]
        slot_tiers_limit = [tickets_required_quantity[tier] for tier in slot_tiers]
        lock_keys = [
            f"{uid}:{ticket.tier_name}"
            for ticket in reservation_result["slot_required"]
        ]

        retry = 0
        non_acquired = None
        while retry < 5:
            non_acquired = await _take_lock(db, lock_keys)
            if not non_acquired:
                break
            retry += 1

        if non_acquired:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Please try again later. We are on shortage"
            )

        # re-reading before triggering replinish
        result = await _reserve_tickets(db, slot_tiers, slot_tiers_limit, uid)
        reserved_ids.update(result["ticket_reserved_ids"])
        if not result["slot_required"]:
            return await _rebook(db, booking, uid, user, tier_prices, reserved_ids)

        # not fulfilled so replinish then again reserve
        await _replinish_tickets(db, slot_tiers, uid)
        result = await _reserve_tickets(db, slot_tiers, slot_tiers_limit, uid)
        reserved_ids.update(result["ticket_reserved_ids"])
        if result["slot_required"]:
            raise HTTPException(status.HTTP_409_CONFLICT, "Please try again later")

        return await _rebook(db, booking, uid, user, tier_prices, reserved_ids)


async def _with_tickets(db: DBSession, rows: list[Record]) -> list[BookingDetail]:
    if not rows:
        return []

    tickets: dict[UUID, list[Ticket]] = {row["uid"]: [] for row in rows}
    grouped: list[Record] = await db.fetch(booking_tickets_query, list(tickets))
    for row in grouped:
        tickets[row["booking_uid"]].append(
            Ticket(tier_name=row["ticket_tier_name"], quantity=row["quantity"])
        )

    return [
        BookingDetail(
            booking_uid=row["uid"],
            amount=row["amount"],
            event_uid=row["event_uid"],
            status=row["status"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            tickets=sorted(tickets[row["uid"]], key=lambda ticket: ticket.tier_name),
        )
        for row in rows
    ]


@router.get("/bookings/{booking_uid}", response_model=BookingDetail)
async def get_booking(booking_uid: UUID, db: DBSession, user: CurrentUser):
    row: Record | None = await db.fetchrow(get_booking_query, booking_uid, user.uid)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "booking not found")
    return (await _with_tickets(db, [row]))[0]


@router.get("/bookings", response_model=BookingListResponse)
async def list_bookings(
    db: DBSession,
    user: CurrentUser,
    after: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
):
    rows: list[Record] = await db.fetch(
        list_bookings_query, user.uid, after or MAX_CURSOR, limit
    )
    bookings = await _with_tickets(db, rows)
    return BookingListResponse(
        bookings=bookings, next=rows[-1]["id"] if len(rows) == limit else None
    )


@router.get("/events/{uid}/bookings", response_model=BookingListResponse)
async def list_booking_of_event(
    uid: UUID,
    db: DBSession,
    user: CurrentUser,
    after: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
):
    rows: list[Record] = await db.fetch(
        list_bookings_of_event_query, uid, user.uid, after or MAX_CURSOR, limit
    )
    bookings = await _with_tickets(db, rows)
    return BookingListResponse(
        bookings=bookings, next=rows[-1]["id"] if len(rows) == limit else None
    )
