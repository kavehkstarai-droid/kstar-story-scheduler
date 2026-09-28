import copy
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
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


RELEASE_URL = "https://github.com/kavehkstarai-droid/kstar-story-scheduler/releases/download/episodes-v1/Kaveh-Star-02-1080p.mp4"
ASSET_URL = "https://release-assets.githubusercontent.com/github-production-release-asset/1234/abcd-1234?sp=r&sig=fake%2Bsignature"
VIDEO_BYTES = b"reviewed-MP4-fixture\x00\xff\r\n"


def release_episode(number=2, **changes):
    return episode(number, video_sha256=hashlib.sha256(VIDEO_BYTES).hexdigest(),
                   media={"release_url": RELEASE_URL, "size_bytes": len(VIDEO_BYTES)}, **changes)


class ReleaseResponse(io.BytesIO):
    def __init__(self, body=VIDEO_BYTES, *, final_url=ASSET_URL, size_header=None, status=200):
        super().__init__(body)
        self.headers = {} if size_header is None else {"Content-Length": size_header}
        self.final_url, self.status, self.bytes_read = final_url, status, 0

    def geturl(self):
        return self.final_url

    def getcode(self):
        return self.status

    def read(self, size=-1):
        data = super().read(size)
        self.bytes_read += len(data)
        return data


def upload_receipt(**changes):
    result = {"message_id": 124, "date": 20000,
              "chat": {"id": -100123, "type": "supergroup", "username": "KavehStar"},
              "video": {"file_id": "reusable-reviewed-video"}}
    result.update(changes)
    return result


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

    def send_file(self, item, path):
        self.send(item, path)
        if Path(path).read_bytes() != VIDEO_BYTES:
            raise AssertionError("Uploader did not receive the verified fixture")
        return upload_receipt()


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


class ReleasePublisherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temp_root = Path(directory.name)
        self.download_calls = []
        self.paths = []

    def downloader(self, events, *, body=VIDEO_BYTES, response_options=None):
        @contextmanager
        def download(item):
            events.append("download")
            def opener(request, **kwargs):
                self.download_calls.append(request)
                self.assertEqual(request.full_url, RELEASE_URL)
                self.assertEqual(request.get_method(), "GET")
                self.assertEqual(request.header_items(), [])
                return ReleaseResponse(body, **(response_options or {}))
            with p.download_release(item, opener=opener, temp_root=self.temp_root) as path:
                self.paths.append(path)
                yield path
            events.append("cleanup")
        return download

    def publish(self, *, item=None, state=None, store=None, telegram=None, downloader=None,
                now=20000, enabled=True):
        events = []
        state = state if state is not None else empty_state()
        store = store if store is not None else FakeStore(state, events=events)
        telegram = telegram if telegram is not None else FakeTelegram(events=events)
        result = p.publish({"enabled": enabled}, {"version": 1, "episodes": [item or release_episode()]},
                           state, store, telegram, clock=lambda: now,
                           release_downloader=downloader or self.downloader(events))
        return result, store, telegram

    def test_exact_release_schema_rejects_input_url_and_size_tricks(self):
        invalid = [RELEASE_URL + "?download=1", RELEASE_URL + "#fragment",
                   RELEASE_URL.replace("https://", "http://"),
                   RELEASE_URL.replace("github.com/", "github.com:443/"),
                   RELEASE_URL.replace("github.com/", "user@github.com/"),
                   RELEASE_URL.replace("github.com/", "github.com.evil.invalid/"),
                   RELEASE_URL.replace("kstar-story-scheduler/", "other-repository/"),
                   RELEASE_URL.replace("episodes-v1/", "../"),
                   RELEASE_URL.replace("episodes-v1/", "v1%2F..%2F"),
                   RELEASE_URL.replace(".mp4", ".mp4.exe"), "\n" + RELEASE_URL]
        for url in invalid:
            with self.subTest(), self.assertRaises(p.Blocked):
                p.validate_media({"release_url": url, "size_bytes": len(VIDEO_BYTES)})
        for size in (True, 0, -1, "10", 1.0, p.MAX_VIDEO_BYTES + 1):
            with self.subTest(), self.assertRaises(p.Blocked):
                p.validate_media({"release_url": RELEASE_URL, "size_bytes": size})
        p.validate_media({"release_url": RELEASE_URL, "size_bytes": p.MAX_VIDEO_BYTES})
        with self.assertRaises(p.Blocked):
            p.validate_media({"release_url": RELEASE_URL, "size_bytes": 10, "file_id": "extra"})

    def test_redirect_allows_only_asset_host_and_discards_inherited_headers(self):
        original = p.urllib.request.Request(RELEASE_URL, headers={"Authorization": "never-forward", "Cookie": "private"})
        handler = p.ReleaseRedirect()
        redirected = handler.redirect_request(original, None, 302, "Found", {}, ASSET_URL)
        self.assertEqual(redirected.full_url, ASSET_URL)
        self.assertEqual(redirected.get_method(), "GET")
        self.assertEqual(redirected.header_items(), [])
        for target in ("http://release-assets.githubusercontent.com/file", "https://evil.invalid/file",
                       "https://release-assets.githubusercontent.com.evil.invalid/file",
                       "https://user@release-assets.githubusercontent.com/file",
                       "https://release-assets.githubusercontent.com:443/file", ASSET_URL + "#fragment",
                       "https://release-assets.githubusercontent.com/../file", RELEASE_URL):
            with self.subTest(), self.assertRaises(p.Blocked):
                handler.redirect_request(original, None, 302, "Found", {}, target)

    def test_download_verifies_before_reservation_then_cleans_up_and_records_file_id(self):
        events = []
        store, telegram = FakeStore(events=events), FakeTelegram(events=events)
        result, _, _ = self.publish(store=store, telegram=telegram, downloader=self.downloader(events))
        self.assertEqual(result, "sent")
        self.assertEqual(events, ["download", "persist", "send", "persist", "cleanup"])
        self.assertEqual(store.persisted["sent"][0]["file_id"], "reusable-reviewed-video")
        self.assertIsNone(store.persisted["dispatching"])
        self.assertTrue(self.paths)
        self.assertFalse(any(path.exists() for path in self.paths))
        self.assertEqual(list(self.temp_root.iterdir()), [])
        p.validate({"enabled": True}, {"version": 1, "episodes": [release_episode()]}, store.persisted)

    def test_wrong_hash_short_long_body_and_invalid_size_header_never_reserve(self):
        for body, options in ((b"x" * len(VIDEO_BYTES), {}), (VIDEO_BYTES[:-1], {}),
                              (VIDEO_BYTES + b"x", {}), (VIDEO_BYTES, {"size_header": str(p.MAX_VIDEO_BYTES + 1)}),
                              (VIDEO_BYTES, {"size_header": "nonsense"})):
            events = []
            store, telegram = FakeStore(events=events), FakeTelegram(events=events)
            with self.subTest(), self.assertRaises(p.Blocked):
                self.publish(store=store, telegram=telegram,
                             downloader=self.downloader(events, body=body, response_options=options))
            self.assertEqual((store.calls, telegram.calls), (0, 0))
            self.assertEqual(list(self.temp_root.iterdir()), [])

    def test_stream_is_bounded_even_without_content_length(self):
        response = ReleaseResponse(VIDEO_BYTES + b"x" * 10000)
        with self.assertRaises(p.Blocked):
            with p.download_release(release_episode(), opener=lambda *a, **k: response,
                                    temp_root=self.temp_root):
                self.fail("An oversized file must not be yielded")
        self.assertEqual(response.bytes_read, len(VIDEO_BYTES) + 1)
        self.assertEqual(list(self.temp_root.iterdir()), [])

    def test_repository_temporary_storage_is_rejected_before_download(self):
        calls = []
        with self.assertRaises(p.Blocked):
            with p.download_release(release_episode(), opener=lambda *a, **k: calls.append(a), temp_root=p.ROOT):
                self.fail("A release asset must never be staged in the repository")
        self.assertEqual(calls, [])

    def test_failed_or_foreign_download_has_no_reservation_or_signed_url_leak(self):
        for options in ({"status": 404}, {"final_url": "https://evil.invalid/private?secret=value"}):
            store, telegram = FakeStore(), FakeTelegram()
            with self.subTest(), self.assertRaises(p.Blocked) as caught:
                self.publish(store=store, telegram=telegram,
                             downloader=self.downloader([], response_options=options))
            self.assertNotIn("secret=value", str(caught.exception))
            self.assertEqual((store.calls, telegram.calls), (0, 0))
        def failure(*args, **kwargs):
            raise RuntimeError("signed-url-secret")
        with self.assertRaises(p.Blocked) as caught:
            with p.download_release(release_episode(), opener=failure, temp_root=self.temp_root):
                pass
        self.assertNotIn("signed-url-secret", str(caught.exception))

    def test_reservation_failure_cleans_verified_file_and_never_uploads(self):
        store, telegram = FakeStore(fail_at=1), FakeTelegram()
        with self.assertRaises(p.Blocked):
            self.publish(store=store, telegram=telegram)
        self.assertEqual(telegram.calls, 0)
        self.assertEqual(list(self.temp_root.iterdir()), [])

    def test_upload_or_receipt_persistence_failure_keeps_reservation_and_blocks_redownload(self):
        for fail_upload, fail_save in ((True, None), (False, 2)):
            store, telegram = FakeStore(fail_at=fail_save), FakeTelegram(fail=fail_upload)
            with self.subTest(), self.assertRaises(p.Blocked):
                self.publish(store=store, telegram=telegram)
            self.assertIsNotNone(store.persisted["dispatching"])
            self.assertEqual(list(self.temp_root.iterdir()), [])
            calls = len(self.download_calls)
            with self.assertRaises(p.Blocked):
                self.publish(state=store.persisted, telegram=telegram, now=999999)
            self.assertEqual(len(self.download_calls), calls)
            self.assertEqual(telegram.calls, 1)

    def test_disabled_unreviewed_and_cooldown_gates_run_before_download(self):
        self.assertEqual(self.publish(enabled=False)[0], "disabled")
        self.assertEqual(self.publish(item=release_episode(reviewed=False))[0], "unreviewed")
        state = empty_state()
        earlier = episode()
        earlier["script_sha256"] = p.script_hash(earlier["script"])
        state["sent"] = [{**p.identity(earlier), "message_id": 123, "sent_at": 20000}]
        state["last_sent_at"] = 20000
        self.assertEqual(self.publish(state=state, now=30799)[0], "too_soon")
        self.assertEqual(self.download_calls, [])
        self.assertEqual(self.publish(state=state, now=30800)[0], "sent")
        self.assertEqual(len(self.download_calls), 1)

    def test_sent_release_is_not_downloaded_or_published_again(self):
        _, store, _ = self.publish()
        count = len(self.download_calls)
        result, _, telegram = self.publish(state=store.persisted, now=999999)
        self.assertEqual((result, telegram.calls), ("empty", 0))
        self.assertEqual(len(self.download_calls), count)

    def test_multipart_upload_preserves_reviewed_bytes_caption_and_destination(self):
        requests = []
        def opener(request, **kwargs):
            requests.append(request)
            return io.BytesIO(json.dumps({"ok": True, "result": upload_receipt()}).encode())
        with p.download_release(release_episode(), opener=lambda *a, **k: ReleaseResponse(), temp_root=self.temp_root) as path:
            result = p.Telegram("fake-token", opener=opener).send_file(release_episode(), path)
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["video"]["file_id"], "reusable-reviewed-video")
        request = requests[0]
        self.assertEqual(request.get_method(), "POST")
        mime = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + request.get_header("Content-type").encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + request.data)
        parts = {part.get_param("name", header="Content-Disposition"): part.get_payload(decode=True)
                 for part in mime.iter_parts()}
        self.assertEqual(parts["video"], VIDEO_BYTES)
        self.assertEqual(parts["caption"].decode(), release_episode()["telegram_caption"])
        self.assertEqual(parts["chat_id"], b"@KavehStar")
        self.assertEqual(parts["supports_streaming"], b"true")

    def test_upload_rechecks_file_hash_before_making_request(self):
        calls = []
        with p.download_release(release_episode(), opener=lambda *a, **k: ReleaseResponse(), temp_root=self.temp_root) as path:
            path.write_bytes(b"modified after download")
            with self.assertRaises(p.Blocked):
                p.Telegram("fake-token", opener=lambda *a, **k: calls.append(a)).send_file(release_episode(), path)
        self.assertEqual(calls, [])

    def test_upload_rejects_wrong_destination_or_nonvideo_receipts(self):
        variants = [upload_receipt(chat={"id": -123, "type": "supergroup", "username": "Other"}),
                    upload_receipt(chat={"id": 123, "type": "private", "username": "KavehStar"}),
                    upload_receipt(video=None), upload_receipt(video={}),
                    upload_receipt(document={"file_id": "document"}), upload_receipt(message_id=0)]
        with p.download_release(release_episode(), opener=lambda *a, **k: ReleaseResponse(), temp_root=self.temp_root) as path:
            for receipt in variants:
                calls = []
                def opener(request, **kwargs):
                    calls.append(request)
                    return io.BytesIO(json.dumps({"ok": True, "result": receipt}).encode())
                with self.subTest(), self.assertRaises(p.Blocked):
                    p.Telegram("fake-token", opener=opener).send_file(release_episode(), path)
                self.assertEqual(len(calls), 1)

    def test_upload_timeout_and_redirect_do_not_retry_or_reveal_token(self):
        calls = []
        def opener(request, **kwargs):
            calls.append(request)
            raise TimeoutError("token-secret https://api.telegram.org/bottoken-secret/sendVideo")
        with p.download_release(release_episode(), opener=lambda *a, **k: ReleaseResponse(), temp_root=self.temp_root) as path:
            with self.assertRaises(p.Blocked) as caught:
                p.Telegram("token-secret", opener=opener).send_file(release_episode(), path)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("token-secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        with self.assertRaises(p.Blocked):
            p.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.invalid/")


if __name__ == "__main__":
    unittest.main()
