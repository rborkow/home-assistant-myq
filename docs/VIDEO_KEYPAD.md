# Video Keypad development

This fork combines the community integration's account setup and HA entities
with the independently tested myQ Keypad Bridge relay protocol. It is an
experimental branch; the full HA deployment and recording validation are in progress.

## Implemented

- Discover `vkp-camera` devices separately from garage doors, using the same
  account inventory requests. Keypad-only accounts can pass setup.
- Resolve the selected camera's Tend alias on demand. Its MAC may be empty in
  the residential API. Require that exact alias in the authenticated broker list.
- Open verified TLS control/media channels, wake the camera, wait for full power,
  then negotiate mode-0 relay video. Blocking TLS work runs outside the HA loop.
- Reuse the existing loopback stream server with an injectable session factory.
  Multiple viewers share a session. Keypad sessions use a 20-second idle grace.
- Cache an IDR for snapshots. Snapshot requests never start a session or read the
  cloud; before the first live keyframe, no snapshot is available. The cache is
  currently in memory, so restarting HA clears it.
- Preserve the existing TC-camera path and account token persistence.
- Provide `myq.record_keypad` for 5–300 seconds of received video, sharing the
  live session. Clips are published in local media only after duration, frame
  count and full-decode validation. Failed or cancelled jobs remove partial files.
  Recording refuses to start below 1 GB free space or at 2 GB of stored clips;
  these are admission checks, not a hard filesystem quota or retention policy.

## Evidence, November 5, 2026

Local checks include strict mypy, Ruff lint and formatting, and the test suite.
A real account inventory returned two garage doors and one Video Keypad.
The new session implementation captured 110 H.264 frames at 1152x864, with the
first frame after 7.21 seconds. FFmpeg decoded the entire capture without error.
The session closed after the bounded test. This was a development-host test,
not yet evidence of an HA camera entity playing on the Yellow.

The fork's source was installed on the Yellow beside the original bridge.
Home Assistant configuration check and restart succeeded. The original bridge
and its credentials have not been replaced. No garage-door commands were tested.
Raw account data, captures and credentials are outside this repository.

Offline replay of the real capture through the new recorder produced a validated
5.0-second MP4 containing 100 decoded frames. This proves the local pipeline,
not live recording on the Yellow. Tests cover cancellation cleanup, low-storage
refusal and sharing one session between a viewer and recorder.

## Remaining acceptance checks

1. Complete the normal HA browser setup and verify keypad entity/device creation.
2. Play and decode video through HA, then exercise shared viewers, cached
   snapshots, idle shutdown, unload/reload and retry after a failed connection.
3. Validate the integrated recorder without introducing a second account refresh
   owner or a competing camera session. Verify usable
   duration, full decode, failure cleanup and storage limits on the Yellow.
4. Decide which existing battery/health diagnostics belong in the integration
   and which remain companion tooling; verify that idle sensor reads do not wake.
5. Produce sanitized deployment/rollback evidence and a contribution breakdown.

Native motion delivery and entitled cloud-clip download remain unverified
research. Neither is advertised as an implemented feature in this fork.

## Candidate contribution split

- Required Tend request metadata headers, backed by the observed 403/200 control.
- Standalone keypad discovery and setup support.
- Keypad session transport plus the shared stream's session-factory extension.
- Battery-aware snapshots/lifecycle and hardware validation.
- Recording in a separate change after its HA architecture is validated.

No upstream proposal or pull request has been submitted. The fork is the place
to validate the complete combination before asking maintainers to review it.
