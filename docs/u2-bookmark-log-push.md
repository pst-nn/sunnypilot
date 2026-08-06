# Private bookmark log push

This fork can send bookmarked `rlog.zst` segments to a private SSH receiver.
It is disabled until device-local credentials and configuration are provisioned.

## Behavior

- `loggerd` marks the exact segment containing a `userBookmark` with
  `user.u2_bookmark=1`.
- `u2_bookmark_uploader` uploads that segment and up to two preceding segments.
- A segment is eligible only after every `.lock` file has disappeared.
- Only `rlog.zst` is sent. Camera video, `qlog.zst`, and audio-only feedback are
  not uploaded.
- A successful file is marked `user.u2_upload=1`; failed transfers remain
  queued and retry when connectivity returns.
- The server verifies the declared size and SHA-256 before an atomic rename.

## Device configuration

Create `/persist/comma/u2_log_upload.json` without committing it:

```json
{
  "host": "upload.example.com",
  "port": 22,
  "user": "comma-upload",
  "device_id": "model-y-comma4"
}
```

The default credential paths are:

- `/persist/comma/u2_log_upload_ed25519` (mode `0600`)
- `/persist/comma/u2_log_upload_known_hosts`

Generate the private key on the comma. Install only its public half on the
server. Pin the server's trusted host key in the known-hosts file; do not use
`StrictHostKeyChecking=no`.

## Server key restriction

The upload account's `authorized_keys` entry must force the receiver and deny
shell, forwarding, PTY, and agent access:

```text
restrict,command="/usr/local/libexec/comma-log-receive" ssh-ed25519 PUBLIC_KEY device-label
```

The receiver defaults to `/srv/comma-logs/<device_id>/<segment>/rlog.zst` and
accepts no command other than its validated upload protocol.
