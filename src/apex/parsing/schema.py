from datetime import datetime

from pydantic import AwareDatetime, BaseModel, Field


class RollPoint(BaseModel):
    """A single GPS sample from a recorded session."""

    timestamp: AwareDatetime
    elapsed_sec: float = Field(ge=0, description="Seconds since the first point")
    lat: float = Field(ge=-90, le=90, description="Decimal degrees")
    lon: float = Field(ge=-180, le=180, description="Decimal degrees")
    speed_mps: float = Field(ge=0)
    speed_mph: float = Field(ge=0)
    altitude_m: float
    garmin_dist_m: float = Field(ge=0, description="Distance as reported by the device")
    cum_dist_m: float = Field(default=0.0, ge=0, description="Haversine distance from the first point")
    bearing_deg: float = Field(default=0.0, ge=0, lt=360, description="Heading from the previous point")


class RollData(BaseModel):
    """Parsed roll data: device metadata, summary stats and ordered points."""

    source_file: str
    full_path: str
    device_id: str
    device_name: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_sec: float = 0.0
    total_dist_m: float = 0.0
    max_speed_mph: float = 0.0
    avg_speed_mph: float = 0.0
    point_count: int = 0
    records: list[RollPoint] = Field(default_factory=list)
