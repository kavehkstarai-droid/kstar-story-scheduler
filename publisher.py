"""Reviewed-video publisher. Standard library only; never generates media.

The first state push is a write-ahead reservation. Uncertain sends intentionally
block the queue, because Telegram sendVideo has no idempotency key.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unicodedata
import urllib.request

DESTINATION = "@KavehStar"
MIN_INTERVAL = 10800
ROOT = Path(__file__).resolve().parent
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
IDENTITY_FIELDS = ("episode_id", "story_id", "script_sha256", "video_sha256")


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
        media = item.get("media", {})
        if set(media) != {"file_id"}:
            raise Blocked("Only a preuploaded Telegram file_id is allowed.")
        value = next(iter(media.values()))
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise Blocked("Invalid media reference.")
        if "file_id" in media and (len(value) > 1024 or re.search(r"\s", value)):
            raise Blocked("Invalid Telegram file_id.")
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
    def __init__(self, token, opener=urllib.request.urlopen):
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


def publish(config, queue, state, store, telegram, clock=time.time):
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
    reference = next_episode["media"]["file_id"]
    pending = copy.deepcopy(state)
    pending["dispatching"] = {**identity(next_episode), "reserved_at": now}
    store.save(pending, "Reserve " + next_episode["episode_id"])
    # Only a successful durable push permits entering this line.
    receipt = telegram.send(next_episode, reference)
    if not positive_int(receipt.get("message_id")):
        raise Blocked("Telegram receipt is invalid; reservation retained.")
    completed_at = max(now, int(clock()), receipt.get("date", 0))
    completed = copy.deepcopy(pending)
    completed["sent"].append({**identity(next_episode), "message_id": receipt["message_id"], "sent_at": completed_at})
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
