# Private development-fork migration

Version `0.4.0.dev2` combines upstream camera work with a private compatibility
layer for the previously installed development fork. The layer preserves the
camera entity and 17 battery, session, recording and control entities. It is not
part of the focused upstream feature PRs.

The migration preserves the existing config entry and OAuth owner, entity and
device associations, dashboard contents, recording preferences, preview cache,
and recording ownership. The old camera unique ID changes to the upstream suffix
while its user-facing entity ID remains unchanged. The installed LAN-only choice
maps to `camera_lan_only=true`; unsupported transport choices require explicit
review rather than silently changing policy.

## Checkpoint and deployment

A single operator stops Home Assistant Core before registry or source changes.
A private checkpoint contains the installed source and original storage files.
Those files may contain credentials and must remain on the HA host.

The source-only deployment helper validates the exact clean Git revision,
checksums, bounded file paths and Python syntax, then swaps the entire component
directory. It runs with the existing HA image, a read-only container root and no
network access. It does not create a second HA runtime or refresh authorization.
Replacing the whole directory removes obsolete modules. A failed source swap or
finalization restores the displaced source before returning failure.

Deployment leaves dashboards unchanged. The preserved record, cancel and explicit
power-refresh buttons continue to use the existing dashboard. Passive snapshots
and ordinary coordinator updates do not wake the keypad. An explicit power
refresh or a new live viewing/recording session may wake it.

## Accepted deployment evidence

At source revision `6fe7847`, local checks and hosted CI pass with 347 tests;
Hassfest and HACS also pass. The canary runs Home Assistant 2026.11.0.

The initial installed-canary checks passed:

- All 18 camera, control and diagnostic entities are present and available.
- All 54 component files match the reviewed source; entity/device identity,
  five recording ledger entries and all three dashboards are preserved.
- Two actual HLS readers ran for 20 and 65 seconds alongside a native 60-second
  recording. The shorter reader exited while the other reader and recording
  continued; both reader captures decoded successfully.
- The completed MP4 contained 1,200 decoded frames at 1152×864 over 60 seconds.
- Two LAN media channels, zero cloud-media connections and one control connection
  were observed. Idle cleanup closed the session and LAN channels. The complete
  validation took approximately 127 seconds.

Recording cancellation also passed: cancellation at approximately 20 seconds
preserved both HLS readers, which decoded successfully. No cancelled clip or
partial file remained, existing clips were unchanged, and both LAN channels and
the control connection closed at idle. The preserved cancel and explicit
power-refresh buttons returned HTTP 200; the subsequent power sample reported
low-power deep sleep with zero consumers and streaming off.

The canary was accepted after a full Core restart. All 18 entities remained
available and all 54 component source hashes still matched `6fe7847`. The same
camera and device IDs, one config entry/OAuth owner, LAN-only policy, 30-day
retention and all three dashboards were preserved. The five prior recording
ledger entries were unchanged and one completed validation clip was newly owned.
The preview JPEG hash, snapshot timestamp, last-recording state and battery state
survived restart exactly. Streaming stayed off with zero consumers.

Version `0.4.0.dev2`, source `6fe7847`, is now deployed on Home Assistant 2026.11.0. These results apply to one
Video Keypad and do not establish ordinary-camera or multi-keypad compatibility.
Cloud authorization, signaling and wake remain required.

## Safe backout

A failed acceptance gate requires stopping Core and restoring checkpoint source
with selective registry/options changes. Preserve the **current** authorization
data, latest recording ownership ledger, completed clips, user entity metadata
and current recording preferences. A newer valid preview can be copied back to
the old store format. Never restore the original whole config-entry file after
any runtime has resumed: its refresh token may be obsolete.

The original dashboard needs no restore because migration does not rewrite it.
Existing clips are never retroactively enrolled in retention. The configured
30-day retention applies to newly owned completed recordings. The restart, passive preview, live HLS, recording, cancellation and idle
acceptance checks have passed. Retain the private checkpoint for selective backout.
