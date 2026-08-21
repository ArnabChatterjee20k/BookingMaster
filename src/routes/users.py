from uuid import uuid4

from asyncpg import Record
from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel

from ..auth.auth import get_token
from ..auth.deps import CurrentUser
from ..config import Config
from ..database.db import DBSession
from ..database.models import Base

router = APIRouter()

class CreateUserRequest(BaseModel):
    email: str
    name: str
    password: str

class UserResponse(Base):
    name: str
    email: str

@router.post("/users", response_model=UserResponse)
async def create_user(user: CreateUserRequest, db: DBSession, response: Response):
    existing_user: Record = await db.fetchrow("select 1 from users where email=$1", user.email)
    if existing_user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "user already exists")
    row: Record = await db.fetchrow(
        "insert into users(uid, email, name, password) values ($1,$2,$3,$4) returning *",
        uuid4(), user.email, user.name, user.password,
    )
    response.set_cookie(Config.auth_cookie_name, get_token(row.get("uid")))
    return UserResponse(**dict(row))

@router.get("/users/me", response_model=UserResponse)
async def get_me(user: CurrentUser):
    return user
