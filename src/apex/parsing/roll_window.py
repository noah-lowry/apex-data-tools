"""Find the roll itself inside a watch recording.

A watch is started well before the buggy launches and stopped well after it
stops, so the recording's own start and duration say little. The roll starts
when the buggy crosses the course's first gate, and ends at the last gate it is
seen crossing: normally the finish, earlier if GPS dropped out before it.

Only true gate-line crossings count. The segmenter falls back to the nearest
point when a gate is not crossed; that fallback is never used as a boundary
here, so a watch that never ran the course has no roll.
"""

from pathlib import Path

from pydantic import AwareDatetime, BaseModel, Field

from .course_segmenter import RollSegmenter
from .schema import CourseSpec, RollData

DEFAULT_COURSE_PATH = Path(__file__).parent / "course_spec.json"


def default_course() -> CourseSpec:
    return CourseSpec.model_validate_json(DEFAULT_COURSE_PATH.read_text())


class RollWindow(BaseModel):
    """The span of a recording between the start gate and the last gate reached."""

    start_time: AwareDatetime = Field(description="Start-gate crossing, interpolated")
    end_time: AwareDatetime
    duration_sec: float
    distance_m: float
    max_speed_mph: float
    end_gate: str = Field(description="Name of the gate the window ends at")
    finished: bool = Field(description="The window ends at the course's final gate")


def detect_roll(roll: RollData, course: CourseSpec) -> RollWindow | None:
    """The roll within `roll`, or None if the start gate was never crossed."""
    segments = RollSegmenter(course).segment(roll)
    first = segments.get(course.segments[0].id)
    if first is None or not first.start_exact:
        return None
    start = first.records[0]

    end, end_gate = None, ""
    for spec in course.segments:
        seg = segments.get(spec.id)
        if seg and seg.end_exact and seg.records[-1].elapsed_sec > start.elapsed_sec:
            end, end_gate = seg.records[-1], spec.end_gate.name
    if end is None:
        return None

    inside = [p for p in roll.records if start.elapsed_sec < p.elapsed_sec < end.elapsed_sec]
    return RollWindow(
        start_time=start.timestamp,
        end_time=end.timestamp,
        duration_sec=end.elapsed_sec - start.elapsed_sec,
        distance_m=end.cum_dist_m - start.cum_dist_m,
        max_speed_mph=max(p.speed_mph for p in (start, *inside, end)),
        end_gate=end_gate,
        finished=end_gate == course.segments[-1].end_gate.name,
    )
