#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import BinaryIO, TextIO


DEFAULT_ROOT = Path("/srv/comma-logs")
MAX_UPLOAD_SIZE = 256 * 1024 * 1024
BLOCK_SIZE = 1024 * 1024
RLOG_NAME = "rlog.zst"

_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.|-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UploadError(Exception):
  pass


def file_sha256(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as stream:
    while block := stream.read(BLOCK_SIZE):
      digest.update(block)
  return digest.hexdigest()


def _parse_command(command: str) -> tuple[str, str, str, int, str]:
  try:
    fields = shlex.split(command)
  except ValueError as exc:
    raise UploadError("invalid command quoting") from exc
  if len(fields) != 6 or fields[0] != "upload":
    raise UploadError("unsupported command")

  _, device, segment, filename, size_text, expected_sha256 = fields
  if any(_COMPONENT_RE.fullmatch(value) is None for value in (device, segment)):
    raise UploadError("invalid path component")
  if filename != RLOG_NAME:
    raise UploadError("unsupported filename")
  if _SHA256_RE.fullmatch(expected_sha256) is None:
    raise UploadError("invalid sha256")
  try:
    size = int(size_text)
  except ValueError as exc:
    raise UploadError("invalid size") from exc
  if not 0 <= size <= MAX_UPLOAD_SIZE:
    raise UploadError("upload size outside limit")
  return device, segment, filename, size, expected_sha256


def receive_upload(command: str, stream: BinaryIO, output: TextIO, root: Path = DEFAULT_ROOT) -> Path:
  device, segment, filename, size, expected_sha256 = _parse_command(command)
  destination_dir = root / device / segment
  destination_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
  destination = destination_dir / filename

  fd, temporary_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".part", dir=destination_dir)
  temporary = Path(temporary_name)
  digest = hashlib.sha256()
  remaining = size
  try:
    with os.fdopen(fd, "wb") as temporary_stream:
      while remaining:
        block = stream.read(min(BLOCK_SIZE, remaining))
        if not block:
          raise UploadError("upload ended before declared size")
        temporary_stream.write(block)
        digest.update(block)
        remaining -= len(block)
      if stream.read(1):
        raise UploadError("upload exceeds declared size")
      temporary_stream.flush()
      os.fsync(temporary_stream.fileno())

    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
      raise UploadError("sha256 mismatch")

    if destination.exists():
      if destination.stat().st_size == size and file_sha256(destination) == expected_sha256:
        temporary.unlink()
        output.write(f"ALREADY {expected_sha256}\n")
        return destination
      raise UploadError("destination already exists with different content")

    temporary.chmod(0o640)
    os.replace(temporary, destination)
    output.write(f"OK {expected_sha256}\n")
    return destination
  except Exception:
    temporary.unlink(missing_ok=True)
    raise


def main() -> int:
  command = os.environ.get("SSH_ORIGINAL_COMMAND", "")
  root = Path(os.environ.get("COMMA_LOG_ROOT", str(DEFAULT_ROOT)))
  try:
    receive_upload(command, sys.stdin.buffer, sys.stdout, root)
    return 0
  except UploadError as exc:
    print(f"ERROR {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
  raise SystemExit(main())
