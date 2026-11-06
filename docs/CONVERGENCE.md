# Upstream camera convergence — 2026-11-06

This is a review/test candidate built from upstream `develop` and PR #11, with
focused contributions applied to the existing asynchronous camera implementation.
It is not yet a drop-in replacement for the previously installed development fork.
There is no second keypad receiver, separate OAuth owner, or Android runtime here.

| Contribution | Upstream PR | State at preparation | Local checks |
| --- | --- | --- | --- |
| Optional LAN-only live video | [#12](https://github.com/bvdcode/home-assistant-myq/pull/12) | Ready for review | 261 tests, Ruff, mypy |
| Bounded RTP assembly and IDR recovery | [#13](https://github.com/bvdcode/home-assistant-myq/pull/13) | Draft; live hardware compatibility pending | 259 tests, Ruff, mypy |
| Passive persistent keypad preview | [#14](https://github.com/bvdcode/home-assistant-myq/pull/14) | Draft; depends on #11 | 261 tests, Ruff, mypy |
| Bounded recording and owned-file retention | [#15](https://github.com/bvdcode/home-assistant-myq/pull/15) | Draft; depends on #11 | 273 tests, Ruff, mypy |
| On-demand keypad wake | [#16](https://github.com/bvdcode/home-assistant-myq/pull/16) | Draft; depends on #11 | 268 tests, Ruff, mypy |

PRs #14 and #15 preserve #11 unchanged and link their follow-up-only diffs. They
should be rebased after #11 lands. The combined branch has 308 passing tests.
Upstream workflows still require maintainer approval. The identical workflows
ran successfully on this fork at combined commit `1fe5bff`:

- [CI: clean dependency installation, Ruff, mypy and 308 tests](https://github.com/rborkow/home-assistant-myq/actions/runs/42279512346)
- [Containerized Hassfest](https://github.com/rborkow/home-assistant-myq/actions/runs/42279512341)
- [HACS repository validation](https://github.com/rborkow/home-assistant-myq/actions/runs/42279512375)

These validate the combined candidate, not each standalone contribution. The
clean runner exposed a missing `ha-ffmpeg` test dependency, now declared. HACS
required enabling Issues and adding topics on this fork; no workflow permission
policies or upstream approval settings were changed.

The RTP contribution was also tested offline against 550 previously captured
keypad media datagrams. It recovered 85 fully decoded H.264 frames at 1152×864,
with no invalid payloads, retained buffer, or live expiry timer. It withheld
frames dependent on a missing packet. Capture timestamps are unavailable, so
replay intervals were synthetic. This is recorded-input coverage, not a live
camera test or proof of TC-camera compatibility. Private captures and keys are
not included in this repository.

## Live candidate validation

The combined candidate at `24a5dbc` was tested in an isolated process on HA Yellow
without replacing the installed integration or refreshing a copied OAuth token.
An explicit REST wake followed by the unchanged upstream session produced 182
fully decoded H.264 frames in a 10-second capture and 586 in a 30-second capture,
both at 1152×864. Both UDP channels matched the authenticated camera LAN address;
there were no cloud-media TCP connections, and the signaling connection closed
after each test. These results validate this combined candidate on one keypad,
not ordinary-camera compatibility or sustained reliability.

The sleeping-keypad test without an explicit wake authenticated and received an
offer but timed out during UDP setup. The candidate now includes on-demand wake
inside shared-session startup, with a 35-second deadline and cancellation cleanup.
The initial 10/30-second tests used a separate probe wake.

The integrated wake helper and shared-stream implementation at `f79d72e` also
passed an isolated live test: two concurrent consumers caused one wake and one
session. One consumer received 202 frames; the other received 102 before being
cancelled, while the first continued. All 202 captured frames decoded cleanly at
1152×864. Source-URL creation was passive, a keyframe was cached, and idle shutdown
closed the session (the probe shortened the idle delay to two seconds). Both
channels were pinned to the camera LAN address and no cloud-media TCP connection
was observed. The probe used the actual candidate helper with a read-only-token
HTTP adapter, not the installed HA entity or its OAuth refresh path. Every loaded
module hash matches the committed candidate; this is not full HA/HLS/MP4 validation.

Full local Hassfest against Home Assistant 2026.7.4 found a missing optional FFmpeg
dependency declaration. That is fixed in the preview, recording, and combined
branches. The pre-wake combined branch passed 308 tests and hosted CI/Hassfest/HACS;
the wake candidate adds lifecycle and bounded-response coverage (323 tests).
The final extra regression prevents starting a wake if unload occurs while a
reconnect is cleaning up the previous session; the wake helper is unchanged from
the successful hardware test.
Private storage migration and dashboard changes remain staged, not applied.

## Before replacing the installed fork

Keep the installed `feature/video-keypad` checkpoint until the candidate has live
hardware coverage and a tested migration. No installation changes were made while
preparing these contributions.

1. Preserve the same config entry and OAuth owner; stop active sessions first.
   Never copy/refresh its authorization into a second runtime.
2. Set `camera_lan_only=true` before candidate setup when migrating the installed
   `transport_policy=lan_only`. Upstream's compatible default is false. Old
   `lan_preferred` and `cloud_relay` modes have no exact mapping in this candidate.
3. Migrate the old camera registry unique ID suffix `_keypad_camera` to `_camera`,
   retaining its entity ID, device association, history and dashboard references.
   Preflight collisions and stop on an existing target ID rather than duplicating it.
4. Copy valid old `.snapshot` storage to `.preview` only when the target is absent
   or older. Retain the old private store for rollback.
5. Preserve duration/cooldown/retention options and the existing recordings ledger,
   including file identities and enrollment timestamps. Never enroll old media.
6. The previous diagnostic and button entities, explicit wake/power sampling, and
   exact device-alias selection are not all ported. Confirm cold-start behavior,
   identity selection, and feature parity; migrate dashboard/automation targets or
   add focused follow-ups before retiring those entities. Native motion and cloud
   clip retrieval remain outside these PRs.
7. Back up source and private config/registry/options/preview/ledger state. Test
   migration idempotence, collisions, reload/restart and rollback. Validate cold
   wake, live HLS, shared recording, cancellation, packet loss and idle shutdown
   without operating a physical garage door.

Keep this private-fork migration separate from upstream feature PRs: upstream
users have never had the old fork's entity IDs or transport option names.
