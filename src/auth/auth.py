import jwt
from datetime import datetime, timedelta, timezone
from ..config import Config


def get_token(user_id):
    expiry = datetime.now(timezone.utc) + timedelta(days=30)
    return jwt.encode(
        {"user_id": str(user_id), "exp": expiry},
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
