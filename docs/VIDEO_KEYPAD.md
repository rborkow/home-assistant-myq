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

The fresh browser flow reached myQ's sign-in page but was deferred because the
user was away. Migration success does not prove end-to-end browser onboarding.
The migration action is an advanced fork feature; its new strings currently use
English fallback text in other locales. Stop the old token owner first. It is
not necessary for users who can complete normal browser sign-in.

## Remaining acceptance checks

1. Complete normal HA browser onboarding when interactive sign-in is available.
2. Finish forced network failure recovery and longer shared-viewer checks.
   Shared-consumer and cancellation behavior also have synthetic tests.
3. Battery/health scope is recorded in [the battery policy](BATTERY_POLICY.md).
   Native power sensors remain follow-up work; the existing bridge's cached
   diagnostics can remain available independently of this camera integration.
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
