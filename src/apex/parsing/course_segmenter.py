from .garmin_parser import haversine_distance
from .schema import CourseSpec, Gate, LatLon, RollData, RollPoint, RollSegment

MIN_POINTS = 4


class RollSegmenter:
    def __init__(self, course: CourseSpec):
        self.course = course

    def segment(self, roll: RollData) -> dict[str, RollSegment]:
        """Segment a roll into {segment_id: RollSegment}

        Segments whose gates aren't crossed in order are omitted."""
        records = roll.records
        if len(records) < MIN_POINTS:
            return {}

        results: dict[str, RollSegment] = {}
        search_idx = 0

        for seg in self.course.segments:
            start_i, start_t, start_exact = _find_gate_crossing(
                records, seg.start_gate, search_idx
            )
            end_i, end_t, end_exact = _find_gate_crossing(
                records, seg.end_gate, max(start_i + 1, search_idx + 1)
            )

            if end_i <= start_i:
                # Forward search failed, retry from the beginning of the roll
                start_i, start_t, start_exact = _find_gate_crossing(
                    records, seg.start_gate, 0
                )
                end_i, end_t, end_exact = _find_gate_crossing(
                    records, seg.end_gate, start_i + 1
                )

            if end_i <= start_i:
                continue

            start_pt = _boundary_point(records, start_i, start_t)
            end_pt = _boundary_point(records, end_i, end_t)
            search_idx = max(search_idx, start_i)

            results[seg.id] = RollSegment(
                segment_id=seg.id,
                start_exact=start_exact,
                end_exact=end_exact,
                records=[start_pt, *records[start_i + 1 : end_i + 1], end_pt],
            )

        return results


def _boundary_point(records: list[RollPoint], i: int, t: float) -> RollPoint:
    if t > 0.0 and i + 1 < len(records):
        return _interpolate(records[i], records[i + 1], t)
    return records[i].model_copy()


def _interpolate(p1: RollPoint, p2: RollPoint, t: float) -> RollPoint:
    """Linearly interpolate between p1 (t=0) and p2 (t=1); bearing takes the shortest arc."""

    def lerp(a: float, b: float) -> float:
        return a + (b - a) * t

    bearing_diff = ((p2.bearing_deg - p1.bearing_deg) + 180.0) % 360.0 - 180.0
    return RollPoint(
        timestamp=p1.timestamp + (p2.timestamp - p1.timestamp) * t,
        elapsed_sec=lerp(p1.elapsed_sec, p2.elapsed_sec),
        lat=lerp(p1.lat, p2.lat),
        lon=lerp(p1.lon, p2.lon),
        speed_mps=lerp(p1.speed_mps, p2.speed_mps),
        speed_mph=lerp(p1.speed_mph, p2.speed_mph),
        altitude_m=lerp(p1.altitude_m, p2.altitude_m),
        garmin_dist_m=lerp(p1.garmin_dist_m, p2.garmin_dist_m),
        cum_dist_m=lerp(p1.cum_dist_m, p2.cum_dist_m),
        bearing_deg=(p1.bearing_deg + bearing_diff * t) % 360.0,
    )


def _find_gate_crossing(
    records: list[RollPoint], gate: Gate, start_from: int = 0
) -> tuple[int, float, bool]:
    """Return (i, t, exact): the crossing lies at fraction t along records[i] -> records[i+1].

    Falls back to (closest_idx, 0.0, False) when no step crosses the gate line.
    """
    start_from = max(0, min(start_from, len(records) - 1))
    q1, q2 = gate.gate_line

    for i in range(start_from, len(records) - 1):
        p1, p2 = records[i], records[i + 1]
        if _segments_intersect(p1, p2, q1, q2):
            return i, _intersection_t(p1, p2, q1, q2), True

    return _closest_point_to_gate(records, gate, start_from), 0.0, False


def _cross(a: LatLon, b: LatLon, c: LatLon) -> float:
    """Signed area of triangle (a, b, c), treating lat/lon as flat 2-D coordinates."""
    return (b.lat - a.lat) * (c.lon - a.lon) - (b.lon - a.lon) * (c.lat - a.lat)


def _on_segment(a: LatLon, b: LatLon, c: LatLon) -> bool:
    return min(a.lat, b.lat) <= c.lat <= max(a.lat, b.lat) and min(
        a.lon, b.lon
    ) <= c.lon <= max(a.lon, b.lon)


def _segments_intersect(p1: LatLon, p2: LatLon, q1: LatLon, q2: LatLon) -> bool:
    """True if the finite step p1->p2 crosses the finite gate line q1->q2 (touching counts)."""
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)

    if d1 * d2 < 0 and d3 * d4 < 0:
        return True

    return (
        (d1 == 0 and _on_segment(q1, q2, p1))
        or (d2 == 0 and _on_segment(q1, q2, p2))
        or (d3 == 0 and _on_segment(p1, p2, q1))
        or (d4 == 0 and _on_segment(p1, p2, q2))
    )


def _intersection_t(p1: LatLon, p2: LatLon, q1: LatLon, q2: LatLon) -> float:
    """Fraction t in [0, 1] along p1->p2 where it meets q1->q2 (0.5 if parallel)."""
    dx_p, dy_p = p2.lat - p1.lat, p2.lon - p1.lon
    dx_q, dy_q = q2.lat - q1.lat, q2.lon - q1.lon

    denom = dx_p * dy_q - dy_p * dx_q
    if abs(denom) < 1e-15:
        return 0.5

    t = ((q1.lat - p1.lat) * dy_q - (q1.lon - p1.lon) * dx_q) / denom
    return max(0.0, min(1.0, t))


def _closest_point_to_gate(
    records: list[RollPoint], gate: Gate, start_from: int
) -> int:
    """Index of the record nearest the gate's center or either line endpoint."""
    targets = (gate, *gate.gate_line)
    best_idx, best_dist = start_from, float("inf")

    for idx in range(start_from, len(records)):
        pt = records[idx]
        d = min(haversine_distance(pt.lat, pt.lon, g.lat, g.lon) for g in targets)
        if d < best_dist:
            best_idx, best_dist = idx, d

    return best_idx
