from fastapi import APIRouter, HTTPException, status, Query
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Annotated, Self
from uuid import UUID, uuid4
from asyncpg import Record
from pydantic import BaseModel, Field, model_validator

from ..auth.deps import CurrentUser
from ..database.db import DBSession
from ..database.models import Base, TicketStatus
from ..database.query import QueryBuilder
from ..database.utils import get_role

router = APIRouter()

class Ticket(BaseModel):
    tier_name: str
    quantity: int

class BookingCreateRequest(BaseModel):
    tickets: list[Ticket]
    booking_uid: UUID


POOL_SIZE = 100

@router.post("/events/{uid}/bookings")
async def create_booking(uid:UUID, booking:BookingCreateRequest, db:DBSession, user: CurrentUser):
    event: Record | None = await db.fetchrow("""select 1 from events where uid=$1 limit 1""", uid)
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")

    ticket_tiers = list(map(lambda b: b.tier_name,booking.tickets))

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

    ticket_tiers_limit = list(map(lambda b: b.quantity,booking.tickets))
    # using lateral query to iterate over tier columns which will help to reference each tier from the cte and apply limit for each
    # each tier matching with each tier via the cross join
    # basically this flow current tiers like (A,B,C) cross join (tickets table where tier is A/B/C and limit for accordingly)
    # we could have used unionall as well but pg allows for update with select only so select * from (select + update) unionall select * from (select + update)
    reserve_query = """
                with current_tiers as (
                    select * from unnest($1::text[], $2::int[]) as x(tier, limit)
                )
                select result.id , result.ticket_tier_name from
                current_tiers cross join lateral (
                    select ticket.id, ticket.ticket_tier_name from
                    tickets where 
                        event_uid=$3 and
                        ticket_tier_name = current_tiers.tier and
                        status=$4
                    order by id
                    limit current_tiers.limit
                    for update skip locked
                ) result
            """

    # no lateral as the limit is constant
    # join on tiers and ticket_tiers on tier-name for update -> update available -> insert batch
    replinish_query = """
        with current_tiers as (
            select * from unnest($1::text[], $2::int[]) as x(tier, limit)
        )
        with target as (
            select id, least(available, $1::int) as batch
            from tickets_tier join current_tiers on current_tiers.tier = tickets_tier.name
            for update of tickets_tier
        )
        bumped as (
            updaet tickets_tier set 
                tickets_tier.avaialable = tickets_tier.avaialable - target.batch,
                updated_at = now()
            where tickets_tier.id = target.id and target.available>0
            returning target.name, target.batch
        )
        insert into tickets (uid, event_uid, ticket_tier_name, status)
        select gen_random_uuid(), $3, bumped.name, $4
        from bumped, generate_series(1, bumped.batch)
        returning ticket_tier_name
    """

    tickets_required_quantity = dict(zip(ticket_tiers, ticket_tiers_limit))
    tickets_slot_available = {tier:0 for tier in ticket_tiers}
    tickets_tier_slot_required = set()
    async with db.transaction():
        tickets_slot: list[Record] = await db.fetch(reserve_query, ticket_tiers, ticket_tiers_limit, uid, TicketStatus.AVAILABLE)
        for ticket in tickets_slot:
            tickets_slot_available[ticket.get("ticket_tier_name")] += 1

        for tier, reserved in tickets_slot_available.items():
            # will be left with only tiers whose tickets are less
            if reserved == tickets_required_quantity[tier]:
                continue
            tickets_tier_slot_required.add(tickets_tier_slot_required)

        if not tickets_tier_slot_required:
            await db.execute("update tickets set booking_uid=$1, updated_at=now() where event_uid=$2 and ticket_tier_name=unnest($3::text[])")
            total_amount = 0
            for ticket in booking.tickets:
                total_amount += (ticket.quantity)
            await db.execute("insert into bookings(uid, event_uid, user_uid, amount, status) values($1, $2, $3, $4, $5)", uuid4(), uid, )

        # inline reserve more for the tier
        lock_keys = [f"{uid}:{tier}" for tier in tickets_tier_slot_required]

        # we could use the unionall as well SELECT key FROM (SELECT 12 AS key, pg_try_advisory_xact_lock(12) AS acquired UNION ALL SELECT 14, pg_try_advisory_xact_lock(14) ) WHERE NOT acquired;
        non_acquired = await db.fetch("""
                SELECT lock_key
                FROM unnest($1::text[]) AS x(lock_key)
                WHERE NOT pg_try_advisory_xact_lock(
                    hashtextextended(lock_key, 0)
                )
            """, lock_keys)

        if non_acquired:
            # someone else is replenishing -- come back and look again rather
            raise HTTPException(status.HTTP_409_CONFLICT, "Please try again later. We are on shortage")

        await db.execute(replinish_query, list(tickets_tier_slot_required), POOL_SIZE)