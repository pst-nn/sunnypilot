import hashlib
import io

import pytest

from openpilot.tools.u2_log_receiver import RLOG_NAME, UploadError, receive_upload


def command_for(data: bytes, *, device="test-comma", segment="route--0", size=None, digest=None):
  if size is None:
    size = len(data)
  if digest is None:
    digest = hashlib.sha256(data).hexdigest()
  return f"upload {device} {segment} {RLOG_NAME} {size} {digest}"


def test_receive_upload_is_atomic_and_idempotent(tmp_path):
  data = b"bookmarked-rlog"
  output = io.StringIO()
  destination = receive_upload(command_for(data), io.BytesIO(data), output, tmp_path)
  assert destination.read_bytes() == data
  assert output.getvalue().startswith("OK ")
  assert list(destination.parent.glob("*.part")) == []

  output = io.StringIO()
  assert receive_upload(command_for(data), io.BytesIO(data), output, tmp_path) == destination
  assert output.getvalue().startswith("ALREADY ")


@pytest.mark.parametrize("command", [
  "",
  "cat /etc/passwd",
  "upload ../escape route--0 rlog.zst 0 " + "0" * 64,
  "upload test route--0 qlog.zst 0 " + "0" * 64,
  "upload test route--0 rlog.zst -1 " + "0" * 64,
  "upload test route--0 rlog.zst 0 invalid",
])
def test_rejects_unsafe_commands(tmp_path, command):
  with pytest.raises(UploadError):
    receive_upload(command, io.BytesIO(), io.StringIO(), tmp_path)


def test_rejects_truncated_extra_and_corrupt_uploads(tmp_path):
  data = b"expected"
  with pytest.raises(UploadError, match="before declared size"):
    receive_upload(command_for(data, size=len(data) + 1), io.BytesIO(data), io.StringIO(), tmp_path)
  with pytest.raises(UploadError, match="exceeds declared size"):
    receive_upload(command_for(data, size=len(data) - 1), io.BytesIO(data), io.StringIO(), tmp_path)
  with pytest.raises(UploadError, match="sha256 mismatch"):
    receive_upload(command_for(data, digest="0" * 64), io.BytesIO(data), io.StringIO(), tmp_path)


def test_refuses_to_overwrite_different_content(tmp_path):
  first = b"first"
  second = b"second"
  receive_upload(command_for(first), io.BytesIO(first), io.StringIO(), tmp_path)
  with pytest.raises(UploadError, match="different content"):
    receive_upload(command_for(second), io.BytesIO(second), io.StringIO(), tmp_path)
