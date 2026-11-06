# Video Keypad development

This fork combines the community integration's account setup and HA entities
with the independently tested myQ Keypad Bridge relay protocol. It is an
experimental branch with live playback and recording verified on an HA Yellow.

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
  persisted at session end and restored after restart (daily-use build).
- Preserve the existing TC-camera path and account token persistence.
- Provide `myq.record_keypad` for 5–300 seconds of received video, sharing the
  live session. Clips are published in local media only after duration, frame
  count and full-decode validation. Failed or cancelled jobs remove partial files.
  Recording refuses to start below 1 GB free space or at 2 GB of stored clips;
  these are admission checks, not a hard filesystem quota. New managed clips have
  configurable retention; existing recordings are never enrolled automatically.

## Evidence, November 5, 2026

Local checks: 280 tests pass, along with strict mypy, Ruff lint and formatting.
A real account inventory returned two garage doors and one Video Keypad.
The new session implementation captured 110 H.264 frames at 1152x864, with the
first frame after 7.21 seconds. FFmpeg decoded the entire capture without error.
The session closed after the bounded test. This was a development-host test,
not yet evidence of an HA camera entity playing on the Yellow.

The fork's source was installed on the Yellow beside the original bridge.
Home Assistant configuration check and restart succeeded. An isolated research
authorization was transferred using the advanced migration flow: HA refreshed it,
validated device discovery and created a loaded entry and standalone keypad camera.
The research token file was retired; HA owns that refresh chain. The original bridge
and its separate credentials have not been replaced. No garage-door commands were tested.
Raw account data, captures and credentials are outside this repository.

Offline replay of the real capture through the new recorder produced a validated
5.0-second MP4 containing 100 decoded frames. This proves the local pipeline,
not live recording on the Yellow. Tests cover cancellation cleanup, low-storage
refusal and sharing one session between a viewer and recorder.

A second offline replay injected a disconnect after frame 40. The recorder opened
two sessions, closed both, and published a fully decoded five-second/100-frame
clip in 7.384 seconds. This validates recorder reconnection and media continuity;
it is not a forced outage of the actual myQ network service.

Subsequent live HA validation:

- `myq.record_keypad`, duration 5: HTTP 200 in 23.53 seconds including wake-up,
  capture and validation; 100 decoded H.264 frames at 1152x864, duration 5.000
  seconds, 854451 bytes, no remaining partial files. Independent full decode passed.
- HA's `camera/stream` HLS endpoint produced a 5.002-second capture with 103 frames.
  Packet DTS values strictly increase. Full decode with a 1/90000 output timebase
  passed; the null muxer's default coarse timebase caused rounding warnings.
- A further five-second recording while HA's stream producer was active completed
  in 7.85 seconds. One control and one media relay connection were observed.
- Stream preloading was disabled. Both relay connections later closed after idle.
  The HA camera proxy returned a cached JPEG (147053 bytes); relay connections
  remained absent at the subsequent check.
- Integration reload returned success. A fresh five-second recording after reload
  completed successfully in 11.92 seconds. A full HA restart preserved the
  authorization: the entry loaded and the camera returned idle.
- Reload during an active recording interrupted the HTTP request (502), removed
  the partial file and left the completed-clip count unchanged at six. The service
  now emits `myq_keypad_recording_finished` with `status: cancelled` on cancellation.
- An isolated sparse-file quota fixture in the Yellow's HA container triggered
  storage refusal before any camera call and left no partial file. Existing media
  was not modified by this test; low free-space behavior is covered by local tests.
- A temporary HA automation received both actual `completed` and `cancelled`
  recording events and emitted the expected test events. Cancellation was triggered
  by integration reload. The test automation was deleted successfully afterward.
- Real transport-interruption recovery is now verified. The Yellow kernel lacks
  `CONFIG_INET_DIAG_DESTROY`, explaining why earlier `ss --kill` attempts silently
  left connections open. A disposable loopback test first verified a replacement
  method using existing container permissions. The live test then reverified the
  HA process owner, socket inode and both endpoints before shutting down exactly
  its media socket. HA opened a new media connection and completed the requested
  20-second recording in 40.29 seconds overall. Independent validation found 400
  decoded frames, duration 20.000 seconds, no partial files and zero relay
  connections after idle. No firewall, permission or camera settings were changed.
  [Sanitized transport evidence](validation/ha-transport-recovery.json).
- A longer concurrency test ran two actual HA HLS readers (20 and 65 seconds)
  alongside a 60-second recording. After the first viewer exited, the other
  viewer and recorder remained active. Sampling throughout the consumer phase
  observed at most one control and one media connection. The recording contained
  1200 frames and exactly 60.000 seconds; the viewer captures contained 400 and
  1300 frames. All three files passed full decode. No partial files or established
  relay connections remained at the end of the 123.3-second test.
  [Sanitized concurrency evidence](validation/ha-concurrent.json).

Fresh browser authorization subsequently completed through HA's native flow.
The user signed in directly with myQ; HA exchanged the callback, validated device
discovery and reported `reauth_successful`. The existing entry remained loaded.
A five-second recording with the new authorization completed in 13.46 seconds.
The entry's Reconfigure action now offers an explicit reconnect path without
deleting entities or replacing working credentials before successful validation.
The migration action is an advanced fork feature; its new strings currently use
English fallback text in other locales. Stop the old token owner first. It is
not necessary for users who can complete normal browser sign-in.

## Validated milestone and follow-up work

1. Fresh browser authorization is verified, including a subsequent live recording.
2. Real media-connection interruption recovery and longer shared-viewer recording
   are verified above, in addition to the synthetic recovery/cancellation tests.
3. Battery/health scope is recorded in [the battery policy](BATTERY_POLICY.md).
   Native power sensors now use a persisted cache with explicit refresh; the
   original bridge is no longer needed for diagnostics.
4. Deployment, rollback and contribution documents are available in this directory.

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

## Native daily-use cutover (2026-11-06)

Version `0.3.0.dev3` replaces the running bridge with native controls and diagnostics.
All 51 deployed source files match the fork. The default Record clip button produced
30 seconds / 600 frames of valid video in 41.77 seconds; cancellation left no partial
files. Existing clips retained their sizes and modification times. The ownership
ledger initially contained only that newly completed clip. A cached JPEG survived
HA restart and reload byte-for-byte; its timestamp and cached power sample were
preserved, with streaming off.

The legacy bridge and generic camera config entries are disabled. The old add-on
is stopped, boot is manual, and watchdog is off; installation and separate credentials
remain for rollback. With the bridge stopped, a native five-second recording produced
100 fully decoded frames in 14.72 seconds. See the [sanitized daily-use evidence](validation/ha-native-daily-use.json).
A cached-preview dashboard example is in [examples/keypad-dashboard.json](examples/keypad-dashboard.json).
