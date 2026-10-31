"""doomscROLL: the feed, leaderboard and roll page, computed from stored analyses.

Everything here is a pure function of the roll list, the stored analysis
summaries and the course, recomputed per request. Comparisons are always
within a buggy: a roll's reference is its buggy's fastest *complete* roll (all
segments measured, finished at the last gate), or the next fastest when the
roll is that best itself. Filters narrow what is listed and ranked on the
leaderboard; they never change a roll's reference.
"""

import statistics
from datetime import datetime

from apex.parsing.schema import CourseSpec

from .db import Store, local_day

PAGE_SIZE = 20
MAX_PAGE_SIZE = 50


def entries(store: Store) -> list[dict]:
    """Every roll, newest first, with its analysis summary (None without data)."""
    analyses = store.roll_analyses()
    out = []
    for roll in store.list_rolls():
        a = analyses.get(roll["id"])
        out.append({**roll, "summary": a["summary"] if a else None, "file_id": a["file_id"] if a else None})
    return out


def _key(name: str) -> str:
    return name.casefold()


def _seg(summary: dict, seg_id: str) -> dict | None:
    """The segment's figures, if it was measured."""
    s = next((x for x in summary["segments"] if x["id"] == seg_id), None)
    return s if s and s["status"] == "ok" else None


def _brief(e: dict) -> dict:
    return {
        "id": e["id"],
        "title": e["title"],
        "buggy": e["buggy"],
        "driver": e["driver"],
        "day": e["day"],
        "total_sec": e["summary"]["total_sec"] if e["summary"] else None,
    }


class Buggies:
    """Per-buggy references over all of a buggy's rolls with data."""

    def __init__(self, all_entries: list[dict], course: CourseSpec):
        self.course = course
        self.by_buggy: dict[str, list[dict]] = {}
        for e in all_entries:
            if e["summary"]:
                self.by_buggy.setdefault(_key(e["buggy"]), []).append(e)
        self.complete = {
            b: sorted((e for e in es if e["summary"]["complete"]), key=lambda e: e["summary"]["total_sec"])
            for b, es in self.by_buggy.items()
        }

    def reference(self, e: dict) -> dict | None:
        """The roll `e` is compared with: its buggy's best, or next best if `e` is the best."""
        for candidate in self.complete.get(_key(e["buggy"]), []):
            if candidate["id"] != e["id"]:
                return candidate
        return None

    def is_best(self, e: dict) -> bool:
        ranked = self.complete.get(_key(e["buggy"]), [])
        return bool(ranked) and ranked[0]["id"] == e["id"]


def _fastest_by_day(all_entries: list[dict]) -> dict[str, int]:
    """{day: id of the fastest complete roll that day}, across all buggies."""
    best: dict[str, dict] = {}
    for e in all_entries:
        s = e["summary"]
        if s and s["complete"] and (e["day"] not in best or s["total_sec"] < best[e["day"]]["summary"]["total_sec"]):
            best[e["day"]] = e
    return {day: e["id"] for day, e in best.items()}


def card(e: dict, buggies: Buggies, fastest_by_day: dict[str, int]) -> dict:
    roll = {k: e[k] for k in ("id", "title", "buggy", "driver", "day", "at_utc", "number", "notes")}
    s = e["summary"]
    if s is None:
        return {**roll, "has_data": False}

    ref = buggies.reference(e)
    best = buggies.is_best(e)
    gaps_from = None if best or ref is None else ref["summary"]
    segments = []
    for seg in s["segments"]:
        ref_seg = _seg(gaps_from, seg["id"]) if gaps_from else None
        segments.append({
            "id": seg["id"],
            "status": seg["status"],
            "time_sec": seg.get("time_sec"),
            "d_start": seg.get("d_start"),
            "d_end": seg.get("d_end"),
            "gap_sec": round(seg["time_sec"] - ref_seg["time_sec"], 3)
            if ref_seg and seg["status"] == "ok" else None,
        })
    badges = []
    if best:
        badges.append("pb")
    if fastest_by_day.get(e["day"]) == e["id"]:
        badges.append("fastest_day")
    if not s["finished"]:
        badges.append("partial")
    if s.get("stopped_sec", 0) >= 3:
        badges.append("stopped")
    return {
        **roll,
        "has_data": True,
        **{k: s[k] for k in ("total_sec", "dist_m", "finished", "complete", "end_gate", "max_mph", "avg_mph", "hill3_mph", "spark", "stopped_sec")},
        "segments": segments,
        "gap_sec": round(s["total_sec"] - gaps_from["total_sec"], 3) if gaps_from and s["complete"] else None,
        "badges": badges,
        "reference": _brief(ref) if gaps_from else None,
    }


def _matches(e: dict, buggy: str | None, driver: str | None, day: str | None) -> bool:
    return (
        (not buggy or _key(e["buggy"]) == _key(buggy))
        and (not driver or (e["driver"] and _key(e["driver"]) == _key(driver)))
        and (not day or e["day"] == day)
    )


def leaderboard(listed: list[dict], course: CourseSpec, today: str) -> dict:
    with_data = [e for e in listed if e["summary"]]
    complete = sorted((e for e in with_data if e["summary"]["complete"]), key=lambda e: e["summary"]["total_sec"])

    segment_bests = []
    for spec in course.segments:
        timed = [(seg["time_sec"], e) for e in with_data if (seg := _seg(e["summary"], spec.id))]
        best = min(timed, key=lambda x: x[0], default=None)
        segment_bests.append({
            "id": spec.id,
            "best": {**_brief(best[1]), "time_sec": best[0]} if best else None,
        })

    per_buggy: dict[str, list[dict]] = {}
    for e in with_data:
        per_buggy.setdefault(e["buggy"], []).append(e)
    buggies = []
    for name, es in per_buggy.items():
        done = [e for e in es if e["summary"]["complete"]]
        bests = [
            min((seg["time_sec"] for e in es if (seg := _seg(e["summary"], spec.id))), default=None)
            for spec in course.segments
        ]
        best = min(done, key=lambda e: e["summary"]["total_sec"]) if done else None
        theoretical = sum(bests) if None not in bests else None
        if theoretical is not None and best:
            # Segment times are stored rounded; never let that put it above the real best.
            theoretical = min(theoretical, best["summary"]["total_sec"])
        buggies.append({
            "buggy": name,
            "rolls": len(es),
            "best": _brief(best) if best else None,
            # The sum of the buggy's best segments: what a roll stitched from them would take.
            "theoretical_sec": round(theoretical, 3) if theoretical is not None else None,
            "median_sec": round(statistics.median(e["summary"]["total_sec"] for e in done), 3) if done else None,
        })
    buggies.sort(key=lambda b: (b["best"] is None, b["best"]["total_sec"] if b["best"] else 0))

    today_best = next((e for e in complete if e["day"] == today), None)
    return {
        "fastest": [_brief(e) for e in complete[:5]],
        "segments": segment_bests,
        "buggies": buggies,
        "today": _brief(today_best) if today_best else None,
    }


def feed(
    store: Store,
    *,
    buggy: str | None = None,
    driver: str | None = None,
    day: str | None = None,
    include_no_data: bool = False,
    cursor: int = 0,
    limit: int = PAGE_SIZE,
) -> dict:
    all_entries = entries(store)
    buggies = Buggies(all_entries, store.course)
    fastest_by_day = _fastest_by_day(all_entries)
    pool = [e for e in all_entries if include_no_data or e["summary"]]
    listed = [e for e in pool if _matches(e, buggy, driver, day)]

    limit = max(1, min(limit, MAX_PAGE_SIZE))
    page = listed[cursor : cursor + limit]
    out = {
        "rolls": [card(e, buggies, fastest_by_day) for e in page],
        "next_cursor": cursor + limit if cursor + limit < len(listed) else None,
        "total": len(listed),
        "options": {
            "buggies": sorted({e["buggy"] for e in pool}, key=_key),
            "drivers": sorted({e["driver"] for e in pool if e["driver"]}, key=_key),
            "days": sorted({e["day"] for e in pool}, reverse=True),
        },
    }
    if cursor == 0:
        today = local_day(datetime.now().astimezone())
        out["leaderboard"] = leaderboard(listed, store.course, today)
    return out


def _rank(value: float, values: list[float]) -> int:
    return 1 + sum(v < value for v in values)


def roll_analysis(store: Store, roll_id: int) -> dict:
    """Everything the roll page shows. Raises db.NotFound for an unknown roll."""
    roll = store.get_roll(roll_id)
    all_entries = entries(store)
    e = next(x for x in all_entries if x["id"] == roll_id)
    if e["summary"] is None:
        return {"roll": roll, "analysis": None}

    buggies = Buggies(all_entries, store.course)
    peers = buggies.by_buggy.get(_key(e["buggy"]), [])
    complete = buggies.complete.get(_key(e["buggy"]), [])
    s = e["summary"]

    totals = [p["summary"]["total_sec"] for p in complete]
    day_totals = [p["summary"]["total_sec"] for p in complete if p["day"] == e["day"]]
    total_stats = {
        "n": len(totals),
        "n_day": len(day_totals),
        "best_sec": totals[0] if totals else None,
        "median_sec": round(statistics.median(totals), 3) if totals else None,
        "rank": _rank(s["total_sec"], totals) if s["complete"] else None,
        "rank_day": _rank(s["total_sec"], day_totals) if s["complete"] else None,
    }
    seg_stats = {}
    for spec in store.course.segments:
        times = sorted(seg["time_sec"] for p in peers if (seg := _seg(p["summary"], spec.id)))
        mine = _seg(s, spec.id)
        seg_stats[spec.id] = {
            "n": len(times),
            "best_sec": times[0] if times else None,
            "median_sec": round(statistics.median(times), 3) if times else None,
            "rank": _rank(mine["time_sec"], times) if mine else None,
        }

    ref = buggies.reference(e)
    file = store.get_file(e["file_id"])
    return {
        "roll": roll,
        "analysis": store.analysis_detail(e["file_id"]),
        "file": {k: file[k] for k in ("id", "filename", "buggy", "device_name", "device_id", "start_utc", "duration_sec")},
        "reference": {**_brief(ref), "label": "next best" if buggies.is_best(e) else "best"} if ref else None,
        "candidates": sorted(
            (
                {**_brief(p), "complete": p["summary"]["complete"]}
                for p in peers if p["id"] != roll_id
            ),
            key=lambda p: (not p["complete"], p["total_sec"]),
        ),
        "stats": {"total": total_stats, "segments": seg_stats},
        "peers": [
            {
                **_brief(p),
                "complete": p["summary"]["complete"],
                "segments": {
                    seg["id"]: {k: seg[k] for k in ("time_sec", "entry_mph", "exit_mph", "min_mph")}
                    for seg in p["summary"]["segments"] if seg["status"] == "ok"
                },
            }
            for p in peers
        ],
    }


def course_info(course: CourseSpec) -> dict:
    def gate(g):
        return {"name": g.name, "line": [[p.lat, p.lon] for p in g.gate_line]}

    return {
        "name": course.course_name,
        "center": [course.reference_center.lat, course.reference_center.lon],
        "segments": [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "color": s.color,
                "bad_gps": s.bad_gps,
                "start_gate": gate(s.start_gate),
                "end_gate": gate(s.end_gate),
            }
            for s in course.segments
        ],
    }
