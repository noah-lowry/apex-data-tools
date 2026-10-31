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
    cum_dist_m: float = Field(
        default=0.0, ge=0, description="Haversine distance from the first point"
    )
    bearing_deg: float = Field(
        default=0.0, ge=0, lt=360, description="Heading from the previous point"
    )


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


class LatLon(BaseModel):
    lat: float = Field(ge=-90, le=90, description="Decimal degrees")
    lon: float = Field(ge=-180, le=180, description="Decimal degrees")


class Gate(LatLon):
    """A named timing gate: a center point plus the line a roll crosses."""

    name: str
    gate_line: tuple[LatLon, LatLon] = Field(
        description="Endpoints of the line crossed to trigger the gate"
    )


class Segment(BaseModel):
    """A stretch of the course between two gates."""

    id: str
    name: str
    description: str
    bad_gps: bool = Field(
        description="GPS is known to be unreliable here (e.g. tree cover)"
    )
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$", description="Hex display color")
    start_gate: Gate
    end_gate: Gate
    target_distance_m: float = Field(gt=0)


class CourseSpec(BaseModel):
    """Course definition: reference point and ordered segments."""

    course_name: str
    reference_center: LatLon
    segments: list[Segment]


class RollSegment(BaseModel):
    """The slice of a roll between a course segment's start and end gates.

    Holds only what was extracted from the roll. Anything computable from
    `records` (entry/exit/min speed, transit time, distance, re-zeroed
    distance/time, ...) is derived downstream, not stored here.
    """

    segment_id: str = Field(
        description="Id of the CourseSpec segment this was cut from"
    )
    start_exact: bool = Field(
        description="Start boundary interpolated at a true gate-line crossing"
    )
    end_exact: bool = Field(
        description="End boundary interpolated at a true gate-line crossing"
    )
    records: list[RollPoint] = Field(
        min_length=2,
        description="Boundary point at the start gate, interior samples, boundary point at the end gate",
    )
