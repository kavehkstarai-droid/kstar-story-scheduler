import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publisher as p


def episode(number=1, **overrides):
    item = {"episode_id": f"ep-{number:03}", "story_id": f"story-{number:03}",
            "title": f"قسمت {number}", "script": f"داستان کاوه، بخش {number}",
            "telegram_caption": f"کاوه استار | قسمت {number}\nبازآفرینی ادبی بر پایهٔ شاهنامهٔ فردوسی.",
            "video_sha256": hashlib.sha256(f"video-{number}".encode()).hexdigest(),
            "reviewed": True, "media": {"file_id": f"fake-file-{number}"}}
    item.update(overrides)
    return item


def empty_state():
    return {"version": 1, "last_sent_at": None, "dispatching": None, "sent": []}


class FakeStore:
    def __init__(self, state=None, fail_at=None, events=None):
        self.persisted = copy.deepcopy(state or empty_state())
        self.calls = 0
        self.fail_at = fail_at
        self.events = events if events is not None else []

    def save(self, state, reason):
        self.calls += 1
        self.events.append("persist")
        if self.calls == self.fail_at:
            raise p.Blocked("simulated persistence failure")
        self.persisted = copy.deepcopy(state)


class FakeTelegram:
    def __init__(self, fail=False, events=None):
        self.calls = 0
        self.fail = fail
        self.events = events if events is not None else []

    def send(self, item, reference):
        self.calls += 1
        self.events.append("send")
        if self.fail:
            raise p.Blocked("simulated ambiguous timeout")
        return {"message_id": 123, "date": 20000}


class PublisherTests(unittest.TestCase):
    def run_publish(self, items=None, state=None, store=None, telegram=None, now=20000, enabled=True):
        state = state if state is not None else empty_state()
        store = store if store is not None else FakeStore(state)
        telegram = telegram if telegram is not None else FakeTelegram()
        result = p.publish({"enabled": enabled}, {"version": 1, "episodes": items if items is not None else [episode()]}, state, store, telegram, clock=lambda: now)
        return result, store, telegram

    def test_durable_reservation_precedes_single_send_and_receipt(self):
        events = []
        result, store, telegram = self.run_publish(store=FakeStore(events=events), telegram=FakeTelegram(events=events))
        self.assertEqual(result, "sent")
        self.assertEqual(events, ["persist", "send", "persist"])
        self.assertEqual(telegram.calls, 1)
        self.assertIsNone(store.persisted["dispatching"])
        self.assertEqual(store.persisted["sent"][0]["message_id"], 123)

    def test_reservation_persistence_failure_never_sends(self):
        telegram = FakeTelegram()
        with self.assertRaises(p.Blocked):
            self.run_publish(store=FakeStore(fail_at=1), telegram=telegram)
        self.assertEqual(telegram.calls, 0)

    def test_timeout_keeps_reservation_and_rerun_does_not_retry(self):
        store, telegram = FakeStore(), FakeTelegram(fail=True)
        with self.assertRaises(p.Blocked):
            self.run_publish(store=store, telegram=telegram)
        self.assertIsNotNone(store.persisted["dispatching"])
        with self.assertRaises(p.Blocked):
            self.run_publish(state=store.persisted, store=store, telegram=telegram, now=50000)
        self.assertEqual(telegram.calls, 1)

    def test_crash_after_reservation_blocks_next_run(self):
        state = empty_state()
        state["dispatching"] = {"episode_id": "ep-001", "reserved_at": 19000}
        telegram = FakeTelegram()
        with self.assertRaises(p.Blocked):
            self.run_publish(state=state, telegram=telegram)
        self.assertEqual(telegram.calls, 0)

    def test_receipt_persistence_failure_does_not_repeat_sent_video(self):
        store, telegram = FakeStore(fail_at=2), FakeTelegram()
        with self.assertRaises(p.Blocked):
            self.run_publish(store=store, telegram=telegram)
        self.assertIsNotNone(store.persisted["dispatching"])
        with self.assertRaises(p.Blocked):
            self.run_publish(state=store.persisted, telegram=telegram, now=50000)
        self.assertEqual(telegram.calls, 1)

    def test_minimum_interval_also_blocks_manual_style_reruns(self):
        _, saved, _ = self.run_publish()
        for now in (19999, 20000, 30799):
            result, store, telegram = self.run_publish(items=[episode(), episode(2)], state=saved.persisted, now=now)
            self.assertEqual(result, "too_soon")
            self.assertEqual((store.calls, telegram.calls), (0, 0))
        result, _, telegram = self.run_publish(items=[episode(), episode(2)], state=saved.persisted, now=30800)
        self.assertEqual((result, telegram.calls), ("sent", 1))

    def test_queue_exhaustion_never_loops_old_episode(self):
        _, saved, _ = self.run_publish()
        result, store, telegram = self.run_publish(state=saved.persisted, now=999999)
        self.assertEqual((result, store.calls, telegram.calls), ("empty", 0, 0))
        self.assertEqual(self.run_publish(items=[])[0], "empty")

    def test_disabled_and_unreviewed_never_send_or_advance(self):
        self.assertEqual(self.run_publish(enabled=False)[0], "disabled")
        result, store, telegram = self.run_publish(items=[episode(reviewed=False), episode(2)])
        self.assertEqual((result, store.calls, telegram.calls), ("unreviewed", 0, 0))

    def test_each_queue_duplicate_identity_is_rejected(self):
        for field in ("episode_id", "story_id", "script", "video_sha256"):
            with self.subTest(field=field), self.assertRaises(p.Blocked):
                self.run_publish(items=[episode(), episode(2, **{field: episode()[field]})])

    def test_normalized_script_duplicate_is_rejected(self):
        self.assertEqual(p.script_hash("كـاوه\u200f   يک"), p.script_hash("كـاوه یک"))
        with self.assertRaises(p.Blocked):
            self.run_publish(items=[episode(script="کاوه\u200cآمد"), episode(2, script="  کاوه   آمد  ")])

    def test_hash_tampering_and_cross_history_duplicates_rejected(self):
        with self.assertRaises(p.Blocked):
            self.run_publish(items=[episode(script_sha256="0" * 64)])
        _, saved, _ = self.run_publish()
        for field in ("story_id", "script", "video_sha256"):
            with self.subTest(field=field), self.assertRaises(p.Blocked):
                self.run_publish(items=[episode(2, **{field: episode()[field]})], state=saved.persisted, now=40000)
        with self.assertRaises(p.Blocked):
            self.run_publish(items=[episode(script="changed")], state=saved.persisted, now=40000)

    def test_url_references_and_destination_override_are_rejected(self):
        with self.assertRaises(p.Blocked):
            self.run_publish(items=[episode(media={"url": "https://example.com/video.mp4"})])
        with self.assertRaises(p.Blocked):
            p.validate({"enabled": True, "destination": "@elsewhere"}, {"version": 1, "episodes": []}, empty_state())

    def test_real_transport_makes_one_call_and_hides_token_on_timeout(self):
        requests = []
        def opener(req, **kwargs):
            requests.append(req)
            raise TimeoutError("secret-token URL")
        transport = p.Telegram("secret-token", opener=opener)
        with self.assertRaises(p.Blocked) as caught:
            transport.send(episode(), "fake-file-1")
        self.assertEqual(len(requests), 1)
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(json.loads(requests[0].data)["chat_id"], "@KavehStar")
        self.assertEqual(json.loads(requests[0].data)["caption"], episode()["telegram_caption"])

    def test_reviewed_caption_is_required_and_utf16_bounded(self):
        for caption in (None, "", "   ", "x" * 1025, "😀" * 513, "bad\x00caption", "bad\ud800"):
            with self.subTest(caption=repr(caption)), self.assertRaises(p.Blocked):
                self.run_publish(items=[episode(telegram_caption=caption)])
        self.assertEqual(p.safe_caption("😀" * 512), "😀" * 512)
        self.assertEqual(p.safe_caption("x" * 1024), "x" * 1024)

    def test_next_poll_after_three_hours_sends_without_waiting_another_three(self):
        _, saved, _ = self.run_publish(now=20120)
        result, _, telegram = self.run_publish(items=[episode(), episode(2)], state=saved.persisted, now=30919)
        self.assertEqual((result, telegram.calls), ("too_soon", 0))
        result, _, telegram = self.run_publish(items=[episode(), episode(2)], state=saved.persisted, now=30920)
        self.assertEqual((result, telegram.calls), ("sent", 1))

    def test_runtime_guard_public_default_branch_only(self):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "schedule", "GITHUB_REPOSITORY": "owner/series", "GITHUB_REF": "refs/heads/main", "GITHUB_TOKEN": "fake"}
        def opener(data):
            return lambda *a, **k: io.BytesIO(json.dumps(data).encode())
        public = {"private": False, "visibility": "public", "default_branch": "main"}
        self.assertEqual(p.verify_public_github(env, opener(public)), "main")
        for changes in ({"GITHUB_REF": "refs/heads/other"}, {"GITHUB_EVENT_NAME": "pull_request"}, {"GITHUB_ACTIONS": "false"}):
            with self.assertRaises(p.Blocked):
                p.verify_public_github({**env, **changes}, opener(public))
        with self.assertRaises(p.Blocked):
            p.verify_public_github(env, opener({**public, "private": True, "visibility": "private"}))


if __name__ == "__main__":
    unittest.main()
