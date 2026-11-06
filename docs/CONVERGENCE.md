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

PRs #14 and #15 preserve #11 unchanged and link their follow-up-only diffs. They
should be rebased after #11 lands. The combined branch has 308 passing tests.
GitHub currently reports `action_required` for upstream workflows; local checks
are not a substitute for upstream CI, Hassfest, or HACS validation.

The RTP contribution was also tested offline against 550 previously captured
keypad media datagrams. It recovered 85 fully decoded H.264 frames at 1152×864,
with no invalid payloads, retained buffer, or live expiry timer. It withheld
frames dependent on a missing packet. Capture timestamps are unavailable, so
replay intervals were synthetic. This is recorded-input coverage, not a live
camera test or proof of TC-camera compatibility. Private captures and keys are
not included in this repository.

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
