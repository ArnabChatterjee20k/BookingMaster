from uuid import UUID
from asyncpg import Connection, Record
from ..cache.cache import Cache


async def get_role(
    db: Connection, cache: Cache, org_uid: UUID, user_uid: UUID
) -> str | None:
    role = await cache.get(f"role:{org_uid}:{user_uid}")
    if role:
        return role
    row: Record | None = await db.fetchrow(
        "select role from memberships where org_uid=$1 and user_uid=$2",
        org_uid,
        user_uid,
    )
    if not row:
        return None
    await cache.set(f"role:{org_uid}:{user_uid}", row.get("role"))
    return row.get("role")
