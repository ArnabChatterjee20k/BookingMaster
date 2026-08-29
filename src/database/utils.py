from uuid import UUID

from asyncpg import Connection, Record


async def get_role(db: Connection, org_uid: UUID, user_uid: UUID) -> str | None:
    row: Record | None = await db.fetchrow(
        "select role from memberships where org_uid=$1 and user_uid=$2",
        org_uid,
        user_uid,
    )
    return row.get("role") if row else None
