import re
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

# geography(point, 4326) comes back from asyncpg as a WKB hex string unless the
# query unwraps it, so select it as ST_AsText(location) / ST_X + ST_Y and this
# model will accept either form.
_EWKT_POINT = re.compile(
    r"^\s*(?:SRID=\d+;)?POINT\s*\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)\s*$",
    re.IGNORECASE,
)

class Point(BaseModel):
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)

    @model_validator(mode="before")
    @classmethod
    def parse(cls, value: Any) -> Any:
        if isinstance(value, str):
            match = _EWKT_POINT.match(value)
            if not match:
                raise ValueError(f"not a WKT point: {value}")
            return {"longitude": match.group(1), "latitude": match.group(2)}
        if isinstance(value, (tuple, list)):
            longitude, latitude = value
            return {"longitude": longitude, "latitude": latitude}
        return value

    def to_wkt(self) -> str:
        return f"SRID=4326;POINT({self.longitude} {self.latitude})"


class Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    uid: UUID
    created_at: datetime | None = None
    updated_at: datetime | None = None
