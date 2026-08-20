import jwt
from fastapi import Depends, Cookie, HTTPException, status
from typing import Annotated
from datetime import datetime, timedelta
from ..config import Config

def get_token(user_id):
    expiry = datetime.now() + timedelta(days=30)
    return jwt.encode(
        {"user_id": user_id, "exp": expiry},
        Config.jwt_secret,
        algorithm="HS256",
    )

def check_token(encoded_jwt) -> str:
    if not encoded_jwt:
        return None
    try:
        decoded = jwt.decode(encoded_jwt, Config.jwt_secret, algorithms=["HS256"])
        return decoded.get("user_id")
    except jwt.PyJWTError:
        return None

def validate_token_in_cookie(token: Annotated[str | None, Cookie(alias=Config.auth_cookie_name)]):
    user_id = check_token(token)
    return user_id