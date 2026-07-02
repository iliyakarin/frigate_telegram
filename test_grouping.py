import unittest

from grouping import PendingGroup, merge_into_pending, split_ready_groups


class TestMergeIntoPending(unittest.TestCase):
    def _review(self, review_id, camera, labels, detections, start, end):
        return {
            "id": review_id,
            "camera": camera,
            "start_time": start,
            "end_time": end,
            "data": {"objects": labels, "detections": detections, "zones": []},
        }

    def test_first_item_creates_new_group(self):
        pending = {}
        merge_into_pending(pending, self._review("r1", "Garage", ["person"], ["e1"], 100, 110), now=110, merge_gap=45)

        self.assertEqual(len(pending), 1)
        group = next(iter(pending.values()))
        self.assertEqual(group.camera, "Garage")
        self.assertEqual(group.labels, {"person"})
        self.assertEqual(group.event_ids, {"e1"})
        self.assertEqual(group.first_start, 100)
        self.assertEqual(group.last_activity_end, 110)

    def test_same_camera_same_label_merges_into_one_group(self):
        pending = {}
        merge_into_pending(pending, self._review("r1", "Garage", ["person"], ["e1"], 100, 110), now=110, merge_gap=45)
        merge_into_pending(pending, self._review("r2", "Garage", ["person"], ["e2"], 130, 140), now=140, merge_gap=45)

        self.assertEqual(len(pending), 1)
        group = next(iter(pending.values()))
        self.assertEqual(group.event_ids, {"e1", "e2"})
        self.assertEqual(group.review_ids, ["r1", "r2"])
        self.assertEqual(group.first_start, 100)
        self.assertEqual(group.last_activity_end, 140)
        self.assertEqual(group.last_seen_at, 140)

    def test_different_camera_never_merges(self):
        pending = {}
        merge_into_pending(pending, self._review("r1", "Garage", ["person"], ["e1"], 100, 110), now=110, merge_gap=45)
        merge_into_pending(pending, self._review("r2", "Backyard", ["person"], ["e2"], 111, 120), now=120, merge_gap=45)

        self.assertEqual(len(pending), 2)

    def test_disjoint_labels_same_camera_do_not_merge(self):
        pending = {}
        merge_into_pending(pending, self._review("r1", "Garage", ["car"], ["e1"], 100, 110), now=110, merge_gap=45)
        merge_into_pending(pending, self._review("r2", "Garage", ["person"], ["e2"], 111, 120), now=120, merge_gap=45)

        self.assertEqual(len(pending), 2)

    def test_overlapping_labels_merge_and_union(self):
        pending = {}
        merge_into_pending(pending, self._review("r1", "Garage", ["person"], ["e1"], 100, 110), now=110, merge_gap=45)
        merge_into_pending(
            pending, self._review("r2", "Garage", ["person", "dog"], ["e2"], 111, 120), now=120, merge_gap=45
        )

        self.assertEqual(len(pending), 1)
        group = next(iter(pending.values()))
        self.assertEqual(group.labels, {"person", "dog"})


class TestSplitReadyGroups(unittest.TestCase):
    def test_group_not_ready_within_merge_gap(self):
        pending = {
            "g1": PendingGroup(
                camera="Garage",
                labels={"person"},
                review_ids=["r1"],
                event_ids={"e1"},
                first_start=100,
                last_activity_end=110,
                last_seen_at=110,
            )
        }
        ready = split_ready_groups(pending, now=130, merge_gap=45, max_span=300)

        self.assertEqual(ready, [])
        self.assertEqual(len(pending), 1)

    def test_group_ready_after_quiet_period(self):
        pending = {
            "g1": PendingGroup(
                camera="Garage",
                labels={"person"},
                review_ids=["r1"],
                event_ids={"e1"},
                first_start=100,
                last_activity_end=110,
                last_seen_at=110,
            )
        }
        ready = split_ready_groups(pending, now=160, merge_gap=45, max_span=300)

        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].camera, "Garage")
        self.assertEqual(pending, {})

    def test_max_span_forces_early_finalize_despite_recent_activity(self):
        pending = {
            "g1": PendingGroup(
                camera="Backyard",
                labels={"car"},
                review_ids=["r1", "r2", "r3"],
                event_ids={"e1", "e2", "e3"},
                first_start=1000,
                last_activity_end=1290,
                last_seen_at=1290,  # just seen, well within merge_gap
            )
        }
        # now - first_start = 310 > max_span=300, must finalize even though quiet-check alone wouldn't fire
        ready = split_ready_groups(pending, now=1300, merge_gap=45, max_span=300)

        self.assertEqual(len(ready), 1)
        self.assertEqual(pending, {})

    def test_multiple_groups_only_ready_ones_are_popped(self):
        pending = {
            "stale": PendingGroup(
                camera="Garage", labels={"person"}, review_ids=["r1"], event_ids={"e1"},
                first_start=0, last_activity_end=10, last_seen_at=10,
            ),
            "fresh": PendingGroup(
                camera="Backyard", labels={"car"}, review_ids=["r2"], event_ids={"e2"},
                first_start=100, last_activity_end=110, last_seen_at=110,
            ),
        }
        ready = split_ready_groups(pending, now=120, merge_gap=45, max_span=300)

        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].camera, "Garage")
        self.assertEqual(list(pending.keys()), ["fresh"])


class TestRegressionFragmentedGarageBurst(unittest.TestCase):
    """Reproduces the real production incident: one continuous person-in-zone
    presence on Garage split by Frigate into 6 separate event IDs across 3
    review items, sent out of chronological order (864 -> 857 -> 899 -> 857 ->
    915). Sanitized from actual frigate-telegram logs captured on 2026-06-30.
    """

    def test_fragmented_burst_merges_into_one_chronologically_bounded_group(self):
        reviews = [
            {
                "id": "rev_a",
                "camera": "Garage",
                "start_time": 1782859810.77,
                "end_time": 1782859826.68,
                "data": {"objects": ["person"], "detections": ["e_809", "e_819", "e_824"], "zones": ["notification_zone"]},
            },
            {
                "id": "rev_b",
                "camera": "Garage",
                "start_time": 1782859858.18,
                "end_time": 1782859869.28,
                "data": {"objects": ["person"], "detections": ["e_857a", "e_857b", "e_864"], "zones": ["notification_zone"]},
            },
            {
                "id": "rev_c",
                "camera": "Garage",
                "start_time": 1782859900.79,
                "end_time": 1782859944.70,
                "data": {"objects": ["person"], "detections": ["e_899", "e_915", "e_926", "e_932", "e_936a", "e_936b"], "zones": ["notification_zone"]},
            },
        ]

        pending = {}
        now = 1782859810.77
        for review in reviews:
            now = max(now, review["end_time"])
            merge_into_pending(pending, review, now=now, merge_gap=45)

        # Gaps between the three review items are ~32s and ~31s -- both under
        # the 45s merge gap, so all three should still be one pending group.
        self.assertEqual(len(pending), 1)

        # Finalize: no further activity for merge_gap seconds.
        ready = split_ready_groups(pending, now=now + 46, merge_gap=45, max_span=300)

        self.assertEqual(len(ready), 1)
        group = ready[0]
        self.assertEqual(group.camera, "Garage")
        self.assertEqual(len(group.event_ids), 12)
        self.assertEqual(group.first_start, 1782859810.77)
        self.assertEqual(group.last_activity_end, 1782859944.70)
        # One notification instead of three fragmented, out-of-order ones.
        self.assertEqual(len(group.review_ids), 3)


if __name__ == "__main__":
    unittest.main()
