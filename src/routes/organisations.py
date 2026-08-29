from uuid import UUID, uuid4
from asyncpg import Record
from fastapi import APIRouter, HTTPException, Query, status, Response
from typing import Annotated
from pydantic import BaseModel

from ..auth.deps import CurrentUser
from ..database.db import DBSession
from ..database.models import Base, MemberRole

router = APIRouter()

class CreateOrgnaisationRequest(BaseModel):
    name: str

class OrganisationResponse(Base):
    name: str
    role: str

class OrgnisationListReponse(BaseModel):
    data: list[OrganisationResponse]
    total: int
    next: int | None = None

class MemberResponse(BaseModel):
    user_uid: UUID
    role: str | None = MemberRole.MEMBER

class CreateMembersRequest(BaseModel):
    members: list[MemberResponse]

class UpdateMemberRequest(BaseModel):
    role: str

class MembersResposne(BaseModel):
    members: list[MemberResponse]

async def _get_role(db: DBSession, org_uid: UUID, user_uid: UUID) -> str | None:
    row: Record = await db.fetchrow(
        "select role from memberships where org_uid=$1 and user_uid=$2", org_uid, user_uid
    )
    return row.get("role") if row else None

@router.post("/organisations", response_model=OrganisationResponse)
async def create_organisation(organisation: CreateOrgnaisationRequest, db: DBSession, user: CurrentUser):
    async with db.transaction():
        org_uid = uuid4()
        org: Record = await db.fetchrow("insert into organisations(uid,name) values($1, $2) returning *", org_uid, organisation.name)
        await db.execute("insert into memberships(uid,org_uid,user_uid,role) values($1,$2,$3,$4)", uuid4(), org_uid, user.uid, MemberRole.OWNER)
        return OrganisationResponse(**org, role=MemberRole.OWNER)

@router.get("/organisations")
async def list_organisations(db: DBSession, user: CurrentUser, after: int = 0, limit: Annotated[int, Query(ge=1, le=100)] = 10):
    # using inner join as we are restricting on the matching rows
    inner_join_query = """
        select o.*, m.role from organisations o
        join memberships m on m.org_uid = o.uid
            where
                m.user_uid = $1
            and
                o.id > $2
        order by o.id asc
        limit $3
    """
    orgs: list[Record] = await db.fetch(inner_join_query, user.uid, after, limit)
    data = [OrganisationResponse(**org) for org in orgs]
    return OrgnisationListReponse(data=data, total=len(data), next=orgs[-1]["id"] if len(orgs) == limit else None)

@router.get("/organisations/{uid}", response_model=OrganisationResponse)
async def get_orginisation(uid: UUID, db: DBSession, user: CurrentUser):
    inner_join_query = """
        select o.*, m.role from organisations o
        join memberships m on m.org_uid = o.uid
            where
                m.user_uid = $1
            and
                o.uid = $2
    """
    org: Record = await db.fetchrow(inner_join_query, user.uid, uid)
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "organisation not found")
    return OrganisationResponse(**org)

@router.delete("/organisations/{uid}")
async def delete_orginisation(uid: UUID, db: DBSession, user: CurrentUser):
    if await _get_role(db, uid, user.uid) != MemberRole.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a owner. Owner can only delete org")
    # TODO: orgnisation should be deleted then with a queue the orphan members
    async with db.transaction():
        await db.execute("delete from organisations where uid=$1", uid)
        await db.execute("delete from memberships where org_uid=$1", uid)
    return Response(status_code=status.HTTP_204_NO_CONTENT)    

@router.post("/organisations/{uid}/members", response_model=MembersResposne)
async def create_members(uid: UUID, db: DBSession, members: CreateMembersRequest, user: CurrentUser):
    if await _get_role(db, uid, user.uid) != MemberRole.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a owner. Owner can only add members")
    await db.executemany(
        "insert into memberships(uid, user_uid, org_uid, role) values ($1,$2,$3,$4)",
        [(uuid4(), m.user_uid, uid, m.role) for m in members.members],
    )
    return MembersResposne(members=members.members)

@router.get("/organisations/{uid}/members", response_model=MembersResposne)
async def list_members(uid: UUID, db: DBSession, user: CurrentUser):
    if await _get_role(db, uid, user.uid) is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member")
    rows: list[Record] = await db.fetch("select user_uid, role from memberships where org_uid=$1", uid)
    return MembersResposne(members=[MemberResponse(**row) for row in rows])

@router.delete("/organisations/{uid}/members/{member_uid}")
async def delete_member(uid: UUID, member_uid: UUID, db: DBSession, user: CurrentUser):
    if await _get_role(db, uid, user.uid) != MemberRole.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a owner. Owner can only remove members")
    if user.uid == member_uid:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Members can't remove themselves. Ask the owner")
    await db.execute("delete from memberships where org_uid=$1 and user_uid=$2", uid, member_uid)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

@router.put("/organisations/{uid}/members/{member_uid}", response_model=MemberResponse)
async def update_member_role(uid: UUID, member_uid: UUID, db: DBSession, user: CurrentUser, member: UpdateMemberRequest):
    if await _get_role(db, uid, user.uid) != MemberRole.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a owner. Owner can only remove members")
    await db.execute(
            "update memberships set role=$1 where user_uid=$2 and org_uid=$3",
            member.role, member_uid, uid
        )
    return MemberResponse(user_uid=member_uid, role=member.role)