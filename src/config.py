import os
from dotenv import load_dotenv

load_dotenv(".env")

def get_value(key, throw=False):
    val = os.environ.get(key, None)
    if throw and val == None:
        raise Exception(f"{key} not present in the env")
    return val

class Config:
    db_uri = get_value("DB_URI")
