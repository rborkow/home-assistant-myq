# Video Keypad contribution plan

Develop and validate the complete experience in this fork before opening upstream
pull requests. Keep the upstream account setup, token refresh and coordinator as
the single source of authorization. Keep the existing TC camera path working.
No upstream proposal or pull request has been submitted.

The working-fork validation milestone is complete on the test Yellow/keypad:
fresh browser authorization, live HLS, validated recording, shared viewers,
idle cleanup, cancellation, reload/restart persistence and recovery after a real
media-socket interruption have evidence in [the validation report](VIDEO_KEYPAD.md).
This is bounded validation of one device/account, not a claim of broad hardware
coverage or measured battery life. The following sequence is the contribution
plan; no upstream acceptance is assumed.

The fork also offers an explicit advanced authorization transfer for validation
and migrations from a stopped client. Keep that separate from the core keypad
PRs: most users should use the upstream browser flow. Transferred tokens are
refreshed and validated before entry creation; the previous client must not reuse
the chain. Migration alone is not evidence of browser onboarding. A subsequent
fresh browser authorization through the native HA reconnect flow succeeded and
was followed by a successful live recording. The explicit reconnect entry point
is a separate candidate contribution from the keypad transport.

## Proposed review sequence

| Change | Scope | Evidence needed before submission |
| --- | --- | --- |
| Tend request headers | Reuse standard application metadata for camera API calls | Sanitized 403/200 comparison and request-header regression test |
| Standalone keypad discovery | Model `vkp-camera` separately from garage doors; allow keypad-only accounts | Inventory tests, removal handling, real HA device/entity creation |
| Keypad live video | Isolated relay protocol and wake lifecycle; session factory in the existing shared stream | Real HA playback, full decode, TLS identity checks, failure cleanup, unload/reload and recovery |
| Battery-aware snapshots and session lifecycle | Cached frames, no wake for snapshots, shared consumers, idle close | Verify cloud/session counters stay unchanged for idle snapshots and stop after the final consumer leaves |
| Local recording | Bounded service, shared session, usable-duration and full-decode validation, cancellation cleanup | Live HA recordings, concurrent viewing, interrupted recording, low-storage refusal and media visibility |

Split commits along these boundaries when the complete implementation passes its
acceptance checks. Some discovery/model changes may need to accompany streaming
so an intermediate PR does not expose a nonfunctional camera. Keep each submitted
PR independently testable; do not force the conceptual split into unusable releases.

## Acceptance on the Yellow

1. Complete native browser authorization and persist tokens through HA's existing
   account flow. Verify refresh ownership and restart persistence; never copy a
   refresh chain into two independent consumers.
2. Confirm one keypad device and camera entity, with existing garage entities
   preserved. Do not operate a physical door in automated tests.
3. Play through HA and decode the resulting video. Record first-frame latency and
   errors without serials, tokens, service URLs containing secrets or raw responses.
4. Open two consumers and record simultaneously. Verify one camera session and
   continued recording when a viewer closes.
5. Close the final consumer and verify relay shutdown after the configured grace.
   Check HA stream preloading: a persistent HA consumer can keep the camera awake
   even after a dashboard closes. Do not claim battery-safe dashboard behavior
   solely from the stream server's lease tests.
6. Request cached snapshots while idle. Confirm no wake, token request or new
   relay session. Persist the last keyframe at session end and expose its timestamp;
   document that the image can be old and is absent before the first live session
   until live video supplies a keyframe.
7. Verify a live recording's requested media duration, frame count and full decode
   on the Yellow. Verify failure/cancellation removes partial files. The current
   1 GB free-space and 2 GB stored-clip checks are admission limits, not retention
   or a hard quota; simultaneous recordings and external disk use require care.
8. Exercise failed connection recovery and integration unload/reload. Confirm no
   orphan worker, encoder or stream listener, and that a later live request works.
9. Save sanitized evidence and deployment/rollback steps. Publish the tested fork
   branch with a clear experimental status before proposing upstream changes.

The current milestone has exercised these checks with the qualifications in the
report: low-free-space refusal is covered by local tests, while quota refusal was
also exercised in an isolated directory inside the Yellow's HA container. The live
failure test shut down one verified media socket; it did not simulate every outage
mode. The two-viewer recording test lasted about two minutes including idle cleanup.

## Battery and recording policy

Use explicit live viewing and bounded recording as defaults. Avoid scheduled
snapshots that wake the keypad. Share the same stream for recording and viewing;
do not create a second account or relay connection for the recorder. The keypad
idle grace is currently 20 seconds, versus the existing TC path's 60 seconds.
This reduces idle session time by design, but battery-life improvement has not
been measured. A longer grace may save repeated wake cycles during short visits.

The recorder targets received frames at 20 fps, rather than claiming continuous
wall-clock coverage across outages. Recovery can extend elapsed time, up to the
bounded deadline. There is no pre-event buffer while the camera sleeps. Triggered
recording may miss the beginning of an event because waking and connecting takes
time. Continuous coverage would require keeping the stream active, with a battery
cost that must be measured before recommending it.

## Research kept out of the initial contributions

Native motion delivery has not been demonstrated. Registering a push receiver is
not evidence that real keypad motion arrives. Cloud recording retrieval has not
been validated with an entitled account; the test account has no video subscription.
Do not advertise motion triggers or cloud-clip download as implemented features.

Authorization remains dependent on myQ's cloud and acceptance of the community
client/App Check approach. Running without an Android app does not make this a
local-only integration or guarantee future access. Reuse upstream authorization
instead of shipping captured credentials or a parallel token-bootstrap mechanism.

Retain a long-lived fork only if the upstream project declines the architecture
or cannot accommodate keypad support. Consider a separate recorder component only
if storage/retention requirements grow beyond a small bounded recording service.

## Daily-use additions in this fork

Keep these reviewable separately from the video protocol: cache-only keypad power
and session diagnostics; explicit refresh/record/cancel controls; persistent cached
previews; configurable recording defaults and cooldown; and ownership-ledger
retention that preserves existing recordings. Retention tests cover older unmanaged
files, fresh files, replaced files, symlinks, disabled cleanup and reload. The
options flow changes recording policy without touching authorization. The supplied
dashboard is deployment guidance, not an automatically installed integration UI.

## Upstream camera overlap — checked 2026-11-06

[PR #11](https://github.com/bvdcode/home-assistant-myq/pull/11), by naisbitt,
adds standalone cameras/Video Keypad discovery, snapshots and live streaming to
`develop`. At review its head was `cd52fc8b45715904a236a50de44c0dbfff2765a4`,
base `11a857191a1d199066be1a14671364ff3e39c409`; it was open and not merged.
The older [PR #7](https://github.com/bvdcode/home-assistant-myq/pull/7) was closed
without a merge, but camera code already exists in `develop`. Do not treat the
closed PR status or the release branch alone as absence of upstream camera work.

The upstream `tend_media.py` already implements UDP hole punching, RTP assembly,
AES decoding and RTCP reports. LAN streaming itself is overlapping work, not a
unique contribution of this fork. PR #11 adds camera selection/discovery on that
existing transport. This overlap should have been reassessed before implementing
another receiver.

Keep the validated installed branch as a checkpoint. Next, integrate upstream's
camera structure and PR #11 (or its merged successor), then port only the remaining
behavior in independently reviewable changes:

1. Battery-aware explicit wake, cached preview persistence and power diagnostics.
2. Exact camera identity selection where metadata permits it.
3. Bounded fragment reordering, malformed-input checks and IDR recovery after loss.
4. Explicit LAN-only policy and visible transport/fallback diagnostics.
5. Bounded recording, cancellation, full-file validation and owned-file retention.

Assess each against current upstream before proposing it. Do not submit a second
complete camera implementation or continue expanding a parallel transport stack.
Research scripts, phone instrumentation, keys and private captures are not part of
an upstream contribution. No upstream PR/comment has been posted by this work.

The current installation remains experimental: one keypad/Yellow was tested,
startup negotiation sometimes needs a retry, and extended battery life has not
been measured. [LAN documentation](LAN_VIDEO.md) distinguishes local media from
cloud control and records keyframe recovery and retransmission limitations.

## Contributions opened — 2026-11-06

The convergence work is now submitted upstream:

- [#12: explicit LAN-only live video](https://github.com/bvdcode/home-assistant-myq/pull/12), ready for review.
- [#13: bounded RTP assembly and recovery](https://github.com/bvdcode/home-assistant-myq/pull/13), draft pending live compatibility validation.
- [#14: passive keypad previews](https://github.com/bvdcode/home-assistant-myq/pull/14), draft follow-up to #11.
- [#15: local recording and owned-file retention](https://github.com/bvdcode/home-assistant-myq/pull/15), draft follow-up to #11.

The [combined upstream candidate](https://github.com/rborkow/home-assistant-myq/tree/converge/upstream-camera)
uses the existing upstream asynchronous transport rather than this fork's separate
receiver. Its `docs/CONVERGENCE.md` records tests, limitations, and the migration
requirements. Do not install it over the current fork without preserving camera
entity IDs, explicit LAN-only settings, cached preview, and recording ownership.
The installed Yellow source remains unchanged during contribution preparation.
