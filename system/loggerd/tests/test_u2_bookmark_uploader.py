import json
import subprocess
import xattr

import pytest

from openpilot.system.loggerd.u2_bookmark_uploader import (
  RLOG_NAME,
  U2_BOOKMARK_ATTR_NAME,
  U2_BOOKMARK_ATTR_VALUE,
  U2_UPLOADED_ATTR_NAME,
  U2_UPLOADED_ATTR_VALUE,
  UploadCandidate,
  UploadConfig,
  credentials_ready,
  list_upload_candidates,
  load_config,
  sha256_file,
  upload_candidate,
)


def make_segment(root, number, *, bookmarked=False, locked=False):
  segment = root / f"route--{number}"
  segment.mkdir()
  rlog = segment / RLOG_NAME
  rlog.write_bytes(f"segment-{number}".encode())
  if bookmarked:
    xattr.setxattr(segment, U2_BOOKMARK_ATTR_NAME, U2_BOOKMARK_ATTR_VALUE)
  if locked:
    (segment / "rlog.lock").touch()
  return segment, rlog


def make_config(tmp_path):
  identity = tmp_path / "id_ed25519"
  identity.write_text("private-test-key")
  identity.chmod(0o600)
  known_hosts = tmp_path / "known_hosts"
  known_hosts.write_text("example.test ssh-ed25519 AAAATEST\n")
  return UploadConfig("example.test", 2847, "comma-upload", "test-comma", identity, known_hosts)


def test_load_config_and_reject_unsafe_values(tmp_path):
  config_path = tmp_path / "upload.json"
  identity = tmp_path / "key"
  known_hosts = tmp_path / "known_hosts"
  config_path.write_text(json.dumps({
    "host": "example.test",
    "port": 2847,
    "user": "comma-upload",
    "device_id": "model-y-comma4",
    "identity_file": str(identity),
    "known_hosts_file": str(known_hosts),
  }))
  config = load_config(config_path)
  assert config is not None
  assert config.port == 2847
  assert config.identity_file == identity

  invalid = json.loads(config_path.read_text())
  invalid["host"] = "example.test;touch /tmp/bad"
  config_path.write_text(json.dumps(invalid))
  with pytest.raises(ValueError):
    load_config(config_path)


def test_credentials_require_private_key_permissions(tmp_path):
  config = make_config(tmp_path)
  assert credentials_ready(config)
  config.identity_file.chmod(0o644)
  assert not credentials_ready(config)


def test_candidates_include_two_prior_segments_and_wait_for_unlock(tmp_path):
  _, rlog0 = make_segment(tmp_path, 0)
  make_segment(tmp_path, 1)
  segment2, _ = make_segment(tmp_path, 2, bookmarked=True, locked=True)
  make_segment(tmp_path, 3)  # outside bookmarked context

  assert {candidate.segment for candidate in list_upload_candidates(tmp_path)} == {"route--0", "route--1"}

  (segment2 / "rlog.lock").unlink()
  assert {candidate.segment for candidate in list_upload_candidates(tmp_path)} == {"route--0", "route--1", "route--2"}

  xattr.setxattr(rlog0, U2_UPLOADED_ATTR_NAME, U2_UPLOADED_ATTR_VALUE)
  assert {candidate.segment for candidate in list_upload_candidates(tmp_path)} == {"route--1", "route--2"}


def test_unbookmarked_segments_are_not_uploaded(tmp_path):
  make_segment(tmp_path, 0)
  make_segment(tmp_path, 1)
  assert list_upload_candidates(tmp_path) == []


def test_upload_streams_file_with_pinned_ssh_options_and_marks_success(tmp_path, monkeypatch):
  config = make_config(tmp_path)
  _, rlog = make_segment(tmp_path, 0)
  candidate = UploadCandidate("route--0", rlog)
  observed = {}

  def fake_run(command, *, stdin, capture_output, check, timeout):
    observed["command"] = command
    observed["data"] = stdin.read()
    return subprocess.CompletedProcess(command, 0, stdout=b"OK test\n", stderr=b"")

  monkeypatch.setattr(subprocess, "run", fake_run)
  assert upload_candidate(config, candidate)
  assert observed["data"] == b"segment-0"
  assert "StrictHostKeyChecking=yes" in observed["command"]
  assert f"UserKnownHostsFile={config.known_hosts_file}" in observed["command"]
  assert observed["command"][-6:] == [
    "upload", "test-comma", "route--0", RLOG_NAME, str(rlog.stat().st_size), sha256_file(rlog),
  ]
  assert xattr.getxattr(rlog, U2_UPLOADED_ATTR_NAME) == U2_UPLOADED_ATTR_VALUE


def test_failed_upload_remains_queued(tmp_path, monkeypatch):
  config = make_config(tmp_path)
  _, rlog = make_segment(tmp_path, 0)

  monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs:
                      subprocess.CompletedProcess(args[0], 255, stdout=b"", stderr=b"network down"))
  assert not upload_candidate(config, UploadCandidate("route--0", rlog))
  with pytest.raises(OSError):
    xattr.getxattr(rlog, U2_UPLOADED_ATTR_NAME)
