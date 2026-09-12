import os
from dotenv import load_dotenv

load_dotenv(".env")


def get_value(key, default=None, throw=True):
    val = os.environ.get(key, default)
    if throw and val == None:
        raise Exception(f"{key} not present in the env")
    return val


class Config:
    db_uri = get_value("DB_URI")
    jwt_secret = get_value("JWT_SECRET")
    db_pool_min = int(get_value("DB_POOL_MIN", 10))
    db_pool_max = int(get_value("DB_POOL_MAX", 20))
    auth_cookie_name = "eventmaster_token"
