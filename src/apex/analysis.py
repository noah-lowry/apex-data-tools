"""Per-recording roll analysis: the numbers and traces behind doomscROLL.

`analyze` turns a parsed recording into two JSON-ready dicts:

* a **summary**: what a feed card and the leaderboard need (total time, per
  segment splits and speeds, a sparkline), and
* a **detail**: the summary plus the full trace inside the roll and every gate
  crossing, for the roll page.

Times and distances are measured from the start-gate crossing, so two rolls
line up at zero. Segments are only reported as `ok` when both of their gates
were truly crossed inside the roll; anything else is `missed`, with null
figures, never a guess. Bump `ANALYSIS_VERSION` whenever a change here alters
the output: stored analyses with an older version are recomputed on startup.
"""

from apex.metrics import compute_segment_metrics
from apex.parsing import RollSegmenter, detect_roll
from apex.parsing.schema import CourseSpec, RollData, RollPoint

ANALYSIS_VERSION = 2

_EPS_SEC = 1e-6
# Below this the buggy is effectively stopped. The final segment (rolling out to
# the Hill 3 pickup) is excluded: slowing to a stop there is the point.
STOPPED_MPH = 2.0


def analyze(recording: RollData, course: CourseSpec) -> tuple[dict, dict] | None:
    """(summary, detail) for the roll inside `recording`, or None if there is none."""
    window = detect_roll(recording, course)
    if window is None:
        return None
    segmented = RollSegmenter(course).segment(recording)
    start = segmented[course.segments[0].id].records[0]
    end = next(
        segmented[s.id].records[-1]
        for s in course.segments
        if s.end_gate.name == window.end_gate and s.id in segmented
    )

    def rel_t(p: RollPoint) -> float:
        return p.elapsed_sec - start.elapsed_sec

    def rel_d(p: RollPoint) -> float:
        return p.cum_dist_m - start.cum_dist_m

    segments, gates = [], []
    for spec in course.segments:
        seg = segmented.get(spec.id)
        inside = (
            seg is not None
            and seg.start_exact
            and seg.end_exact
            and rel_t(seg.records[0]) >= -_EPS_SEC
            and rel_t(seg.records[-1]) <= rel_t(end) + _EPS_SEC
        )
        if not inside:
            segments.append({"id": spec.id, "status": "missed"})
            continue
        m = compute_segment_metrics(seg)
        first, last = seg.records[0], seg.records[-1]
        segments.append({
            "id": spec.id,
            "status": "ok",
            "time_sec": round(m.transit_time_sec, 3),
            "dist_m": round(m.distance_m, 1),
            "entry_mph": round(m.entry_speed_mph, 2),
            "exit_mph": round(m.exit_speed_mph, 2),
            "min_mph": round(m.min_speed_mph, 2),
            "max_mph": round(m.max_speed_mph, 2),
            "avg_mph": round(m.avg_speed_mph, 2),
            "delta_mph": round(m.delta_speed_mph, 2),
            "turn_deg": round(m.heading_delta_deg, 1),
            "samples": m.point_count - 2,  # minus the two interpolated boundary points
            "t_start": round(rel_t(first), 3),
            "t_end": round(rel_t(last), 3),
            "d_start": round(rel_d(first), 1),
            "d_end": round(rel_d(last), 1),
        })
        for gate, point in ((spec.start_gate, first), (spec.end_gate, last)):
            if not gates or gates[-1]["name"] != gate.name:
                gates.append({
                    "name": gate.name,
                    "t": round(rel_t(point), 3),
                    "d": round(rel_d(point), 1),
                    "mph": round(point.speed_mph, 2),
                })

    points = [start, *(p for p in recording.records if start.elapsed_sec < p.elapsed_sec < end.elapsed_sec), end]
    trace = {
        "t": [round(rel_t(p), 3) for p in points],
        "d": [round(rel_d(p), 1) for p in points],
        "mph": [round(p.speed_mph, 2) for p in points],
        "lat": [round(p.lat, 7) for p in points],
        "lon": [round(p.lon, 7) for p in points],
    }

    final = next((s for s in segments if s["id"] == course.segments[-1].id and s["status"] == "ok"), None)
    before_final = final["t_start"] if final else float("inf")
    stopped = sum(
        t1 - t0
        for t0, t1, v0, v1 in zip(trace["t"], trace["t"][1:], trace["mph"], trace["mph"][1:], strict=False)
        if v0 < STOPPED_MPH and v1 < STOPPED_MPH and t1 <= before_final
    )

    summary = {
        "version": ANALYSIS_VERSION,
        "start_utc": window.start_time.isoformat(),
        "total_sec": round(window.duration_sec, 3),
        "dist_m": round(window.distance_m, 1),
        "avg_mph": round(window.distance_m / window.duration_sec * 2.23694, 2),
        "max_mph": round(window.max_speed_mph, 2),
        "finished": window.finished,
        "end_gate": window.end_gate,
        # What the Hill 3 pusher inherits; only meaningful if the roll got there.
        "hill3_mph": round(end.speed_mph, 2) if window.finished else None,
        "complete": window.finished and all(s["status"] == "ok" for s in segments),
        "segments": segments,
        "spark": [[d, v] for d, v in zip(trace["d"], trace["mph"], strict=True)],
        "samples": len(points) - 2,
        # Seconds spent stopped on the course before the rollout (traffic, a
        # stall); 0 for a clean roll.
        "stopped_sec": round(stopped, 1),
    }
    return summary, {**summary, "trace": trace, "gates": gates}
