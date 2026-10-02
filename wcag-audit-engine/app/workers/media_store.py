"""Generated media kept on this node and handed out by link.

A Veo clip is several megabytes. As base64 inside the JSON body it is of no
use to an agent -- it cannot put it in its context -- and it is a heavy body
for every router between us and the buyer. So the bytes are written here and
the result carries a link that works for KEEP_SECONDS, unguessable (a random
128-bit name) and served by /work/media/{name}. Files older than that are
removed whenever a new one is written.
"""

import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

# Google keeps a Veo clip for 2 days; so do we.
KEEP_SECONDS = 48 * 3600
_NAME = re.compile(r"^[0-9a-f]{32}\.(mp4|png|jpg|wav|mp3)$")
MEDIA_TYPES = {"mp4": "video/mp4", "png": "image/png", "jpg": "image/jpeg",
               "wav": "audio/wav", "mp3": "audio/mpeg"}


def media_dir() -> str:
    return os.environ.get("WORKER_MEDIA_DIR", "/data/media")


def _base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "https://hubvibe-io.com").rstrip("/")


def _prune(directory: str) -> None:
    cutoff = time.time() - KEEP_SECONDS
    try:
        for entry in os.scandir(directory):
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    os.remove(entry.path)
            except OSError:
                pass
    except OSError:
        pass


def save(data: bytes, extension: str) -> dict:
    """Write `data` and return {"url", "expires_at"}. Raises OSError when it
    cannot be stored, so the caller can deliver inline instead."""
    if extension not in MEDIA_TYPES:
        raise ValueError(f"unsupported media type: {extension}")
    directory = media_dir()
    os.makedirs(directory, exist_ok=True)
    _prune(directory)
    name = f"{uuid.uuid4().hex}.{extension}"
    final = os.path.join(directory, name)
    partial = final + ".part"
    with open(partial, "wb") as handle:
        handle.write(data)
    os.replace(partial, final)
    expires = datetime.fromtimestamp(time.time() + KEEP_SECONDS, tz=timezone.utc)
    return {"url": f"{_base_url()}/work/media/{name}",
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}


def path_for(name: str) -> Optional[str]:
    """The file behind a link, or None when the name is malformed, unknown or
    older than KEEP_SECONDS."""
    if not _NAME.match(name or ""):
        return None
    path = os.path.join(media_dir(), name)
    try:
        if time.time() - os.stat(path).st_mtime > KEEP_SECONDS:
            return None
    except OSError:
        return None
    return path


def media_type(name: str) -> str:
    return MEDIA_TYPES.get(name.rsplit(".", 1)[-1], "application/octet-stream")
