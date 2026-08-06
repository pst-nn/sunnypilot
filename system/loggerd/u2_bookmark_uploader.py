#!/usr/bin/env python3
from __future__ import annotations

import errno
import hashlib
import json
import random
import re
import stat
import subprocess
import threading
import xattr
from dataclasses import dataclass
from pathlib import Path

from openpilot.common.swaglog import cloudlog
from openpilot.system.hardware.hw import Paths


CONFIG_PATH = Path("/persist/comma/u2_log_upload.json")
DEFAULT_IDENTITY_PATH = Path("/persist/comma/u2_log_upload_ed25519")
DEFAULT_KNOWN_HOSTS_PATH = Path("/persist/comma/u2_log_upload_known_hosts")

U2_BOOKMARK_ATTR_NAME = "user.u2_bookmark"
U2_BOOKMARK_ATTR_VALUE = b"1"
U2_UPLOADED_ATTR_NAME = "user.u2_upload"
U2_UPLOADED_ATTR_VALUE = b"1"

RLOG_NAME = "rlog.zst"
CONTEXT_SEGMENTS = 2
MAX_BACKOFF = 300.0

_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_USER_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.|-]{1,128}$")


@dataclass(frozen=True)
class UploadConfig:
  host: str
  port: int
  user: str
  device_id: str
  identity_file: Path = DEFAULT_IDENTITY_PATH
  known_hosts_file: Path = DEFAULT_KNOWN_HOSTS_PATH


@dataclass(frozen=True)
class UploadCandidate:
  segment: str
  path: Path


def _valid_absolute_path(value: object, default: Path) -> Path:
  path = default if value is None else Path(str(value))
  if not path.is_absolute():
    raise ValueError("credential paths must be absolute")
  return path


def load_config(path: Path = CONFIG_PATH) -> UploadConfig | None:
  if not path.is_file():
    return None

  raw = json.loads(path.read_text())
  host = str(raw["host"])
  port = int(raw["port"])
  user = str(raw["user"])
  device_id = str(raw["device_id"])
  if _HOST_RE.fullmatch(host) is None:
    raise ValueError("invalid upload host")
  if not 1 <= port <= 65535:
    raise ValueError("invalid upload port")
  if _USER_RE.fullmatch(user) is None:
    raise ValueError("invalid upload user")
  if _COMPONENT_RE.fullmatch(device_id) is None:
    raise ValueError("invalid device id")

  return UploadConfig(
    host=host,
    port=port,
    user=user,
    device_id=device_id,
    identity_file=_valid_absolute_path(raw.get("identity_file"), DEFAULT_IDENTITY_PATH),
    known_hosts_file=_valid_absolute_path(raw.get("known_hosts_file"), DEFAULT_KNOWN_HOSTS_PATH),
  )


def credentials_ready(config: UploadConfig) -> bool:
  if not config.identity_file.is_file() or not config.known_hosts_file.is_file():
    return False
  # Refuse to use a private key readable by group/other users.
  return (stat.S_IMODE(config.identity_file.stat().st_mode) & 0o077) == 0


def _get_xattr(path: Path, name: str) -> bytes | None:
  try:
    return xattr.getxattr(path, name)
  except OSError as exc:
    missing_xattr = exc.errno == errno.ENODATA or (hasattr(errno, "ENOATTR") and exc.errno == errno.ENOATTR)
    if missing_xattr or exc.errno == errno.ENOENT:
      return None
    raise


def _segment_number(name: str) -> tuple[str, int] | None:
  route, separator, segment = name.rpartition("--")
  if not separator or _COMPONENT_RE.fullmatch(route) is None:
    return None
  try:
    return route, int(segment)
  except ValueError:
    return None


def list_upload_candidates(log_root: Path) -> list[UploadCandidate]:
  if not log_root.is_dir():
    return []

  selected: set[str] = set()
  for entry in log_root.iterdir():
    parsed = _segment_number(entry.name)
    if not entry.is_dir() or parsed is None:
      continue
    if _get_xattr(entry, U2_BOOKMARK_ATTR_NAME) != U2_BOOKMARK_ATTR_VALUE:
      continue

    route, segment = parsed
    for number in range(max(0, segment - CONTEXT_SEGMENTS), segment + 1):
      selected.add(f"{route}--{number}")

  candidates: list[UploadCandidate] = []
  for segment in selected:
    segment_dir = log_root / segment
    if not segment_dir.is_dir() or any(path.name.endswith(".lock") for path in segment_dir.iterdir()):
      continue
    rlog = segment_dir / RLOG_NAME
    if not rlog.is_file() or _get_xattr(rlog, U2_UPLOADED_ATTR_NAME) == U2_UPLOADED_ATTR_VALUE:
      continue
    candidates.append(UploadCandidate(segment=segment, path=rlog))

  return sorted(candidates, key=lambda candidate: (candidate.path.stat().st_ctime_ns, candidate.segment))


def sha256_file(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as stream:
    while block := stream.read(1024 * 1024):
      digest.update(block)
  return digest.hexdigest()


def upload_candidate(config: UploadConfig, candidate: UploadCandidate, timeout: float = 300.0) -> bool:
  if not credentials_ready(config):
    return False
  if _COMPONENT_RE.fullmatch(candidate.segment) is None:
    raise ValueError("invalid segment name")

  size = candidate.path.stat().st_size
  digest = sha256_file(candidate.path)
  command = [
    "/usr/bin/ssh",
    "-T",
    "-p", str(config.port),
    "-i", str(config.identity_file),
    "-o", "BatchMode=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", f"UserKnownHostsFile={config.known_hosts_file}",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    f"{config.user}@{config.host}",
    "upload", config.device_id, candidate.segment, RLOG_NAME, str(size), digest,
  ]

  with candidate.path.open("rb") as stream:
    result = subprocess.run(command, stdin=stream, capture_output=True, check=False, timeout=timeout)

  success = result.returncode == 0 and result.stdout.startswith((b"OK ", b"ALREADY "))
  if success:
    xattr.setxattr(candidate.path, U2_UPLOADED_ATTR_NAME, U2_UPLOADED_ATTR_VALUE)
    cloudlog.event("u2_bookmark_upload_success", segment=candidate.segment, size=size, sha256=digest)
  else:
    cloudlog.event("u2_bookmark_upload_failed", segment=candidate.segment, size=size,
                   returncode=result.returncode, stderr=result.stderr[-500:].decode(errors="replace"))
  return success


def main(exit_event: threading.Event | None = None) -> None:
  if exit_event is None:
    exit_event = threading.Event()

  backoff = 1.0
  while not exit_event.is_set():
    try:
      config = load_config()
      if config is None or not credentials_ready(config):
        exit_event.wait(30.0)
        continue

      candidates = list_upload_candidates(Path(Paths.log_root()))
      if not candidates:
        backoff = 1.0
        exit_event.wait(10.0)
        continue

      if upload_candidate(config, candidates[0]):
        backoff = 1.0
        exit_event.wait(0.1)
      else:
        exit_event.wait(backoff + random.uniform(0.0, backoff))
        backoff = min(backoff * 2.0, MAX_BACKOFF)
    except Exception:
      cloudlog.exception("u2 bookmark uploader error")
      exit_event.wait(backoff + random.uniform(0.0, backoff))
      backoff = min(backoff * 2.0, MAX_BACKOFF)


if __name__ == "__main__":
  main()
