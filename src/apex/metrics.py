"""Metrics derived deterministically from a RollSegment."""

from pydantic import BaseModel, Field

from apex.parsing.schema import RollSegment

MPS_TO_MPH = 2.23694


class SegmentMetrics(BaseModel):
    segment_id: str
    entry_speed_mph: float = Field(description="Speed at the start gate")
    exit_speed_mph: float = Field(description="Speed at the end gate")
    min_speed_mph: float
    max_speed_mph: float
    delta_speed_mph: float = Field(description="Exit minus entry speed")
    avg_speed_mph: float = Field(description="Distance over transit time")
    transit_time_sec: float
    distance_m: float
    heading_in_deg: float
    heading_out_deg: float
    heading_delta_deg: float = Field(description="Signed shortest-arc change in heading, [-180, 180)")
    point_count: int
    seg_dist_m: list[float] = Field(description="Per-point distance since the start gate")
    seg_time_sec: list[float] = Field(description="Per-point time since the start gate")


def compute_segment_metrics(segment: RollSegment) -> SegmentMetrics:
    """Compute performance metrics and re-zeroed distance/time profiles for a RollSegment.

    Average speed is distance / transit time (position-derived), not the mean of
    the device's filtered speed field, which lags during deceleration.
    """
    points = segment.records
    start, end = points[0], points[-1]

    transit_time = end.elapsed_sec - start.elapsed_sec
    distance = end.cum_dist_m - start.cum_dist_m
    avg_speed = distance / transit_time * MPS_TO_MPH if transit_time > 0 else start.speed_mph

    speeds = [p.speed_mph for p in points]

    return SegmentMetrics(
        segment_id=segment.segment_id,
        entry_speed_mph=start.speed_mph,
        exit_speed_mph=end.speed_mph,
        min_speed_mph=min(speeds),
        max_speed_mph=max(speeds),
        delta_speed_mph=end.speed_mph - start.speed_mph,
        avg_speed_mph=avg_speed,
        transit_time_sec=transit_time,
        distance_m=distance,
        heading_in_deg=start.bearing_deg,
        heading_out_deg=end.bearing_deg,
        heading_delta_deg=(end.bearing_deg - start.bearing_deg + 180.0) % 360.0 - 180.0,
        point_count=len(points),
        seg_dist_m=[p.cum_dist_m - start.cum_dist_m for p in points],
        seg_time_sec=[p.elapsed_sec - start.elapsed_sec for p in points],
    )
