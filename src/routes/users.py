from fastapi import APIRouter, HTTPException, Response, Request
from fastapi import status
from pydantic import BaseModel
from uuid import uuid4
from ..config import Config
from ..database.db import DBSession
from ..database.models import Base
from ..auth.auth import get_token
from ..auth.deps import OptionalUser
from asyncpg import Record

router = APIRouter()


class CreateUserRequest(BaseModel):
    email: str
    name: str
    password: str

class CreateUserSession(BaseModel):
    email: str
    password: str


class UserResponse(Base):
    name: str
    email: str


@router.post("/users", response_model=UserResponse)
async def create_user(user: CreateUserRequest, db: DBSession, response: Response):
    existing_user: Record = await db.fetchrow(
        "select 1 from users where email=$1", user.email
    )
    if existing_user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "user already exists")
    row: Record = await db.fetchrow(
        "insert into users(uid, email, name, password) values ($1,$2,$3,$4) returning *",
        uuid4(),
        user.email,
        user.name,
        user.password,
    )
    response.set_cookie(Config.auth_cookie_name, get_token(row.get("uid")))
    return UserResponse(**row)

@router.get("/users", response_model=UserResponse)
async def get_user(user: OptionalUser):
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised")

    return UserResponse(**user.model_dump())

@router.post("/users/sessions", response_model=UserResponse)
async def create_session(user: OptionalUser, session: CreateUserSession, db:DBSession, request: Request, response: Response):
    if user:
        request.cookies.clear()
    user: Record = await db.fetchrow("select * from users where email=$1 and password=$2", session.email, session.password)
    if not user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Wrong email or password")
    response.set_cookie(Config.auth_cookie_name, get_token(user.get("uid")))
    return UserResponse(**user)