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
    auth_cookie_name = "eventmaster_token"