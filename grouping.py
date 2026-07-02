"""Groups rapid-fire Frigate review items into single notification units.

Frigate can split one continuous physical event (e.g. a person lingering in a
zone) into several review items a few seconds/minutes apart. This module
merges review items on the same camera with overlapping labels into a single
PendingGroup, and holds that group until activity goes quiet, so exactly one
notification covers the whole event.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PendingGroup:
    camera: str
    labels: set[str]
    review_ids: list[str] = field(default_factory=list)
    event_ids: set[str] = field(default_factory=set)
    first_start: float = 0.0
    last_activity_end: float = 0.0
    last_seen_at: float = 0.0


def _find_mergeable_key(pending: dict[str, PendingGroup], camera: str, labels: set[str]) -> str | None:
    for key, group in pending.items():
        if group.camera == camera and not group.labels.isdisjoint(labels):
            return key
    return None


def merge_into_pending(
    pending: dict[str, PendingGroup],
    review: dict,
    now: float,
    merge_gap: int,
) -> None:
    """Merge a Frigate review item into an existing pending group, or start a new one.

    A review item joins an existing group only if that group is on the same
    camera and shares at least one label. Time proximity is enforced
    implicitly: split_ready_groups() must be called every poll tick with the
    same merge_gap, which reaps any group that's gone quiet — so a group can
    only still be "pending" here if it was active within the last merge_gap
    seconds, making a separate explicit gap check on this path redundant.
    """
    camera = review.get("camera", "")
    labels = set(review.get("data", {}).get("objects", []))
    review_id = review.get("id", "")
    detections = set(review.get("data", {}).get("detections", []))
    start_time = review.get("start_time", now)
    end_time = review.get("end_time") or start_time

    key = _find_mergeable_key(pending, camera, labels)
    if key is None:
        pending[review_id] = PendingGroup(
            camera=camera,
            labels=labels,
            review_ids=[review_id],
            event_ids=detections,
            first_start=start_time,
            last_activity_end=end_time,
            last_seen_at=now,
        )
        return

    group = pending[key]
    group.labels |= labels
    group.review_ids.append(review_id)
    group.event_ids |= detections
    group.first_start = min(group.first_start, start_time)
    group.last_activity_end = max(group.last_activity_end, end_time)
    group.last_seen_at = now


def split_ready_groups(
    pending: dict[str, PendingGroup],
    now: float,
    merge_gap: int,
    max_span: int,
) -> list[PendingGroup]:
    """Pop and return groups ready to finalize: quiet for merge_gap seconds,
    or exceeding max_span since they first started (runaway protection).
    """
    ready_keys = [
        key
        for key, group in pending.items()
        if (now - group.last_seen_at) >= merge_gap or (now - group.first_start) >= max_span
    ]
    return [pending.pop(key) for key in ready_keys]
