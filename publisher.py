"""Reviewed-video publisher. Standard library only; never generates media.

The first state push is a write-ahead reservation. Uncertain sends intentionally
block the queue, because Telegram sendVideo has no idempotency key.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.parse import urlsplit
import urllib.request
import uuid

DESTINATION = "@KavehStar"
MIN_INTERVAL = 10800
ROOT = Path(__file__).resolve().parent
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
IDENTITY_FIELDS = ("episode_id", "story_id", "script_sha256", "video_sha256")
MAX_VIDEO_BYTES = 49_000_000
RELEASE_URL = re.compile(
    r"https://github\.com/kavehkstarai-droid/kstar-story-scheduler/releases/download/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}/[A-Za-z0-9][A-Za-z0-9._-]{0,159}\.mp4\Z"
)
RELEASE_ASSET_HOST = "release-assets.githubusercontent.com"
MAX_RECEIPT_BYTES = 65536


class Blocked(Exception):
    """Safe, non-secret diagnostic that may be printed in Actions logs."""


def normalized_script(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(str.maketrans({"ي": "ی", "ك": "ک", "\u200c": " "}))
    # Ignore direction controls and decorative zero-width characters.
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return " ".join(text.split())


def script_hash(text):
    return hashlib.sha256(normalized_script(text).encode("utf-8")).hexdigest()


def identity(episode):
    return {key: episode[key] for key in IDENTITY_FIELDS}


def positive_int(value):
    return type(value) is int and value > 0


def safe_caption(value):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise Blocked("Every episode needs a nonempty reviewed telegram_caption.")
    try:
        units = len(value.encode("utf-16-le")) // 2
    except UnicodeError:
        raise Blocked("Telegram caption contains invalid Unicode.") from None
    if units > 1024:
        raise Blocked("Telegram caption exceeds 1024 UTF-16 units; revise it before review.")
    return value


def validate_media(media):
    if not isinstance(media, dict):
        raise Blocked("Invalid media reference.")
    if set(media) == {"file_id"}:
        value = media["file_id"]
        if not isinstance(value, str) or not 1 <= len(value) <= 1024 or re.search(r"\s", value):
            raise Blocked("Invalid Telegram file_id.")
    elif set(media) == {"release_url", "size_bytes"}:
        if not isinstance(media["release_url"], str) or not RELEASE_URL.fullmatch(media["release_url"]):
            raise Blocked("Only the approved repository's canonical MP4 release URL is allowed.")
        if not positive_int(media["size_bytes"]) or media["size_bytes"] > MAX_VIDEO_BYTES:
            raise Blocked("Release video size must be positive and at most 49000000 bytes.")
    else:
        raise Blocked("Media must be a Telegram file_id or an approved release URL with exact size_bytes.")


def allowed_asset_redirect(url):
    if not isinstance(url, str) or len(url) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in url) or "\\" in url:
        return False
    try:
        parsed = urlsplit(url)
        # A signed query is allowed only on the server-provided asset redirect;
        # no query is allowed on the URL supplied in queue.json.
        return (parsed.scheme == "https" and parsed.netloc == RELEASE_ASSET_HOST
                and not parsed.fragment and parsed.path.startswith("/")
                and re.fullmatch(r"/[A-Za-z0-9/._-]+", parsed.path) is not None
                and not any(part in (".", "..") for part in parsed.path.split("/")))
    except (ValueError, TypeError):
        return False


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    # GitHub documents this host for release assets:
    # https://docs.github.com/en/actions/reference/runners/github-hosted-runners
    max_redirections = 2
    max_repeats = 1

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if (req.get_method() != "GET" or code not in (301, 302, 303, 307, 308)
                or not (RELEASE_URL.fullmatch(req.full_url) or allowed_asset_redirect(req.full_url))
                or not allowed_asset_redirect(newurl)):
            raise Blocked("Release download redirect was rejected.")
        # Never copy Authorization, cookies, or any other request headers to an
        # asset host. This client has no cookie jar or authentication handlers.
        return urllib.request.Request(newurl, method="GET")


def release_open(request, *, timeout):
    opener = urllib.request.build_opener(ReleaseRedirect())
    opener.addheaders = []
    return opener.open(request, timeout=timeout)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Blocked("Telegram redirects are not permitted.")


def telegram_open(request, *, timeout):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


@contextmanager
def download_release(episode, *, opener=release_open, temp_root=None):
    """Yield an exact reviewed MP4 in temporary storage, always cleaning it up."""
    media = episode["media"]
    validate_media(media)
    reviewed_hash = episode.get("video_sha256")
    if "release_url" not in media or not isinstance(reviewed_hash, str) or not HASH.fullmatch(reviewed_hash):
        raise Blocked("A release download needs its reviewed video hash.")
    base = Path(temp_root or os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()).resolve()
    if base == ROOT.resolve() or ROOT.resolve() in base.parents:
        raise Blocked("Release downloads must use temporary storage outside the repository.")
    try:
        with tempfile.TemporaryDirectory(prefix="kstar-release-", dir=base) as directory:
            path = Path(directory) / "video.mp4"
            request = urllib.request.Request(media["release_url"], method="GET")
            digest, size = hashlib.sha256(), 0
            with opener(request, timeout=30) as response:
                final_url = response.geturl()
                if (response.getcode() != 200
                        or not (final_url == media["release_url"] or allowed_asset_redirect(final_url))):
                    raise Blocked("Release download did not return an allowed successful response.")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise Blocked("Encoded release download was rejected.")
                length = response.headers.get("Content-Length")
                if length is not None and (not re.fullmatch(r"[0-9]+", length) or int(length) != media["size_bytes"]):
                    raise Blocked("Release download size header does not match the reviewed size.")
                with path.open("wb") as output:
                    while True:
                        chunk = response.read(min(65536, media["size_bytes"] - size + 1))
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > media["size_bytes"] or size > MAX_VIDEO_BYTES:
                            raise Blocked("Release download exceeded its reviewed size limit.")
                        digest.update(chunk)
                        output.write(chunk)
                if size != media["size_bytes"] or digest.hexdigest() != episode["video_sha256"]:
                    raise Blocked("Release video does not match its reviewed size and SHA-256.")
            yield path
    except Blocked:
        raise
    except Exception:
        # HTTP errors may contain signed redirect URLs. Never expose them.
        raise Blocked("Release download or temporary-file handling failed; stop and inspect state before retrying.") from None


def validate(config, queue, state):
    if set(config) != {"enabled"} or type(config["enabled"]) is not bool:
        raise Blocked("Invalid config; only boolean enabled is allowed.")
    if queue.get("version") != 1 or not isinstance(queue.get("episodes"), list):
        raise Blocked("Invalid queue schema.")
    if state.get("version") != 1 or not isinstance(state.get("sent"), list):
        raise Blocked("Invalid persistent state schema.")
    for key in ("dispatching", "last_sent_at"):
        if key not in state:
            raise Blocked("Persistent state fields are missing.")
    previous = state["last_sent_at"]
    if previous is not None and not positive_int(previous):
        raise Blocked("Invalid last_sent_at.")
    seen = {key: set() for key in IDENTITY_FIELDS}
    sent_by_id = {}
    for item in state["sent"]:
        for key in IDENTITY_FIELDS:
            value = item.get(key)
            rule = IDENTIFIER if key.endswith("_id") else HASH
            if not isinstance(value, str) or not rule.fullmatch(value):
                raise Blocked("Invalid sent-history identity.")
            if value in seen[key]:
                raise Blocked("Duplicate material already exists in sent history.")
            seen[key].add(value)
        if not positive_int(item.get("message_id")) or not positive_int(item.get("sent_at")):
            raise Blocked("Sent history is missing its receipt or timestamp.")
        sent_by_id[item["episode_id"]] = item
    latest = max((x["sent_at"] for x in state["sent"]), default=None)
    if previous != latest:
        raise Blocked("last_sent_at does not match sent history; reconcile state.")
    queue_seen = {key: set() for key in IDENTITY_FIELDS}
    episodes = []
    for source in queue["episodes"]:
        item = copy.deepcopy(source)
        if not isinstance(item.get("script"), str) or not normalized_script(item["script"]):
            raise Blocked("Every queued episode needs its reviewed script.")
        calculated = script_hash(item["script"])
        if item.get("script_sha256", calculated) != calculated:
            raise Blocked("A queued script hash does not match its text.")
        item["script_sha256"] = calculated
        for key in IDENTITY_FIELDS:
            value = item.get(key)
            rule = IDENTIFIER if key.endswith("_id") else HASH
            if not isinstance(value, str) or not rule.fullmatch(value):
                raise Blocked("Invalid episode/story ID or SHA-256.")
            if value in queue_seen[key]:
                raise Blocked("Queue contains duplicate episode, story, script, or video.")
            queue_seen[key].add(value)
        if type(item.get("reviewed")) is not bool:
            raise Blocked("Every episode needs an explicit reviewed boolean.")
        if not isinstance(item.get("title"), str) or not item["title"].strip():
            raise Blocked("Every episode needs a title.")
        safe_caption(item.get("telegram_caption"))
        validate_media(item.get("media"))
        old = sent_by_id.get(item["episode_id"])
        if old:
            if identity(old) != identity(item):
                raise Blocked("A previously sent episode was changed; reconcile manually.")
        elif any(item[key] in seen[key] for key in IDENTITY_FIELDS):
            raise Blocked("New queue material repeats a previously sent episode.")
        episodes.append(item)
    return episodes, sent_by_id


def verify_public_github(env=os.environ, opener=urllib.request.urlopen):
    """Check live repository visibility before any Telegram request."""
    if env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_EVENT_NAME") not in ("schedule", "workflow_dispatch"):
        raise Blocked("Publishing is allowed only by the scheduled/default-branch GitHub workflow.")
    repo = env.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise Blocked("Invalid GitHub repository context.")
    token = env.get("GITHUB_TOKEN")
    if not token:
        raise Blocked("GitHub token is missing.")
    req = urllib.request.Request(
        "https://api.github.com/repos/" + repo,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "User-Agent": "kaveh-star-publisher"},
    )
    try:
        with opener(req, timeout=20) as response:
            data = json.load(response)
    except Exception:
        raise Blocked("Cannot verify repository visibility; publishing blocked.") from None
    branch = data.get("default_branch")
    if data.get("private") is not False or data.get("visibility") != "public":
        raise Blocked("Only a PUBLIC repository may run this publisher.")
    if not isinstance(branch, str) or env.get("GITHUB_REF") != "refs/heads/" + branch:
        raise Blocked("Publishing is restricted to the default branch.")
    return branch


class GitState:
    def __init__(self, root, branch):
        self.root, self.branch = Path(root), branch

    def save(self, state, reason):
        target = self.root / "state.json"
        temporary = self.root / "state.json.tmp"
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
        commands = [
            ["git", "add", "--", "state.json"],
            ["git", "-c", "user.name=Kaveh Star Publisher", "-c", "user.email=publisher@users.noreply.github.com", "commit", "--only", "-m", reason, "--", "state.json"],
            ["git", "push", "origin", "HEAD:refs/heads/" + self.branch],
        ]
        for command in commands:
            result = subprocess.run(command, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if result.returncode:
                # Git error output could contain credentials: never print it.
                raise Blocked("State commit/push failed; stop and inspect/reconcile the reservation.")


class Telegram:
    def __init__(self, token, opener=telegram_open):
        if not token:
            raise Blocked("TELEGRAM_BOT_TOKEN secret is missing.")
        self._token, self._opener = token, opener

    def send(self, episode, media):
        caption = safe_caption(episode.get("telegram_caption"))
        body = json.dumps({"chat_id": DESTINATION, "video": media, "caption": caption, "supports_streaming": True}).encode("utf-8")
        req = urllib.request.Request("https://api.telegram.org/bot" + self._token + "/sendVideo", data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            # Exactly one request. A timeout must NEVER cause an automatic retry.
            with self._opener(req, timeout=90) as response:
                data = json.load(response)
            result = data.get("result", {})
            if data.get("ok") is not True or not positive_int(result.get("message_id")):
                raise ValueError("No confirmed Telegram receipt")
            return result
        except Exception:
            # urllib exceptions contain the token-bearing URL; suppress the chain.
            raise Blocked("Telegram outcome is unconfirmed; reservation retained. Reconcile before any further send.") from None


    def send_file(self, episode, path):
        """Upload already-reviewed bytes exactly once, without HTTP redirects."""
        try:
            with Path(path).open("rb") as source:
                video = source.read(MAX_VIDEO_BYTES + 1)
            if (not 0 < len(video) <= MAX_VIDEO_BYTES or len(video) != episode["media"]["size_bytes"]
                    or hashlib.sha256(video).hexdigest() != episode["video_sha256"]):
                raise ValueError("Verified download changed before upload")
            caption = safe_caption(episode.get("telegram_caption"))
            boundary = "kstar" + uuid.uuid4().hex
            while boundary.encode() in video or boundary in caption:
                boundary = "kstar" + uuid.uuid4().hex
            parts = []
            for name, value in (("chat_id", DESTINATION), ("caption", caption), ("supports_streaming", "true")):
                parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode("utf-8"))
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="episode.mp4"\r\n'
                          'Content-Type: video/mp4\r\n\r\n').encode("ascii"))
            parts.extend((video, f"\r\n--{boundary}--\r\n".encode("ascii")))
            request = urllib.request.Request("https://api.telegram.org/bot" + self._token + "/sendVideo",
                data=b"".join(parts), headers={"Content-Type": "multipart/form-data; boundary=" + boundary}, method="POST")
            # One POST only. An ambiguous response must never trigger a retry.
            with self._opener(request, timeout=180) as response:
                payload = response.read(MAX_RECEIPT_BYTES + 1)
                if len(payload) > MAX_RECEIPT_BYTES:
                    raise ValueError("Oversized receipt")
                data = json.loads(payload)
            result = data.get("result", {})
            chat, received_video = result.get("chat", {}), result.get("video", {})
            file_id = received_video.get("file_id")
            if (data.get("ok") is not True or not positive_int(result.get("message_id"))
                    or not positive_int(result.get("date")) or chat.get("type") != "supergroup"
                    or not isinstance(chat.get("username"), str)
                    or chat["username"].casefold() != DESTINATION[1:].casefold()
                    or type(chat.get("id")) is not int or chat["id"] >= 0
                    or "document" in result or not isinstance(file_id, str)
                    or not 1 <= len(file_id) <= 1024 or re.search(r"\s", file_id)):
                raise ValueError("No matching video delivery receipt")
            return result
        except Exception:
            raise Blocked("Telegram upload outcome is unconfirmed; reservation retained. Reconcile before any further send.") from None


def publish(config, queue, state, store, telegram, clock=time.time, release_downloader=download_release):
    episodes, sent_by_id = validate(config, queue, state)
    if not config["enabled"]:
        return "disabled"
    if state["dispatching"] is not None:
        raise Blocked("Unresolved dispatching reservation: manual reconciliation required.")
    next_episode = next((x for x in episodes if x["episode_id"] not in sent_by_id), None)
    if next_episode is None:
        return "empty"
    if not next_episode["reviewed"]:
        return "unreviewed"
    now = int(clock())
    if state["last_sent_at"] is not None and now - state["last_sent_at"] < MIN_INTERVAL:
        return "too_soon"
    is_release = "release_url" in next_episode["media"]
    # Download failures are safe to revisit; no send reservation exists yet.
    # Disabled, exhausted, unreviewed and cooldown-gated queues never download.
    with (release_downloader(next_episode) if is_release else nullcontext(None)) as path:
        pending = copy.deepcopy(state)
        pending["dispatching"] = {**identity(next_episode), "reserved_at": int(clock())}
        store.save(pending, "Reserve " + next_episode["episode_id"])
        # Only a successful durable push permits a Telegram send.
        receipt = (telegram.send_file(next_episode, path) if is_release
                   else telegram.send(next_episode, next_episode["media"]["file_id"]))
        if not positive_int(receipt.get("message_id")):
            raise Blocked("Telegram receipt is invalid; reservation retained.")
        completed_at = max(now, int(clock()), receipt.get("date", 0))
        completed = copy.deepcopy(pending)
        delivered = {**identity(next_episode), "message_id": receipt["message_id"], "sent_at": completed_at}
        if is_release:
            delivered["file_id"] = receipt["video"]["file_id"]
        completed["sent"].append(delivered)
        completed["last_sent_at"] = completed_at
        completed["dispatching"] = None
        store.save(completed, "Record delivery " + next_episode["episode_id"])
    return "sent"


def read_inputs():
    return [json.loads((ROOT / name).read_text(encoding="utf-8-sig")) for name in ("config.json", "queue.json", "state.json")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate local manifests without network or sending")
    parser.add_argument("--hash-script", type=Path, help="Print normalized script SHA-256; no network")
    args = parser.parse_args()
    try:
        if args.hash_script:
            print(script_hash(args.hash_script.read_text(encoding="utf-8-sig")))
            return 0
        config, queue, state = read_inputs()
        validate(config, queue, state)
        if args.check:
            print("Manifests valid; enabled=" + str(config["enabled"]).lower() + "; episodes=" + str(len(queue["episodes"])))
            return 0
        if not config["enabled"]:
            return 0
        branch = verify_public_github()
        result = publish(config, queue, state, GitState(ROOT, branch), Telegram(os.environ.get("TELEGRAM_BOT_TOKEN")))
        if result == "sent":
            print("One reviewed episode delivered to " + DESTINATION + "; receipt committed.")
        # Empty, disabled, unreviewed, and timing-gated queues remain quiet.
        return 0
    except Blocked as error:
        print("BLOCKED: " + str(error), file=sys.stderr)
        return 1
    except Exception:
        # Unexpected errors also stop, without exposing credentials or media URLs.
        print("BLOCKED: Unexpected error. Inspect persistent state before rerunning.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
