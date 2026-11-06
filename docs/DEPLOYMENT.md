# Experimental Video Keypad deployment

Use the `feature/video-keypad` branch of `rborkow/home-assistant-myq`. This is a
development fork, not an upstream release. Copy its `custom_components/myq`
directory into HA's `custom_components`, run the HA configuration check and restart
HA. Keep a backup of any previous `myq` directory. The separate original keypad
bridge can remain installed as a fallback, but avoid simultaneous camera sessions.

Set up MyQ in Devices & services using normal browser sign-in. An existing entry's
**Reconfigure** menu offers fresh sign-in while keeping its current credentials
until the replacement authorization passes validation. For an advanced
migration, stop the client that owns the refresh token, select **Transfer an
existing authorization**, and provide the account email and refresh token. HA
validates refresh and discovery, then stores the newly issued tokens. Retire the
old client's token file; do not run two refresh owners for the same chain. Migration
is optional and does not require signing out of separately authorized phone apps.

The keypad appears as a separate device with a camera entity. Keep camera stream
preloading disabled for battery use. Snapshots are cached from previous live video;
they do not wake the keypad and can be stale. The last received keyframe is saved
at session end and restored after restart; the Snapshot time sensor shows its age. Live video defaults to direct LAN transport; myQ/Tend cloud authorization, wake
and rendezvous remain required. See [LAN video](LAN_VIDEO.md) for explicit
LAN-only, LAN-preferred and cloud-relay policies and network requirements.

## Recording

Call `myq.record_keypad` targeting the keypad camera, with `duration` between 5
and 300 seconds. If omitted, the configurable default is 30 seconds. The device
also offers Record clip and Cancel recording buttons. Cancellation stops only the
recording consumer; other viewers retain their leases. A configurable 60-second
cooldown between recording starts limits repeated automation triggers. Duration measures received video at the negotiated 20 fps. Wake-up
and bounded reconnection can extend elapsed time. No audio or sleeping-camera
pre-event buffer is provided.

Validated files appear under the configured local media directory's `myq-keypad`
folder. HA emits `myq_keypad_recording_finished` with the entity ID and status:

- `completed`: includes path, decoded frames, media seconds and wall seconds.
- `failed`: includes an error type without service credentials or raw responses.
- `cancelled`: recording was interrupted, for example during integration reload.

Only completed, validated clips are published. Partial files are removed on handled
failure/cancellation; abrupt host power loss may leave partial files. Existing clips are never automatically enrolled in retention. Newly completed recordings are registered in a
persistent ownership ledger and expire after 30 days by default. In the integration's
Configure menu, change retention to 0 to disable deletion, or select 1–365 days.
Cleanup runs at setup and hourly, checks file identity, and ignores unregistered,
replaced, and symlinked files. It never scans old files into ownership. New recordings are refused below 1 GB free space
or at 2 GB of stored MP4s; this is an admission check, not a hard quota.

## Yellow validation and rollback

Version `0.4.0.dev1` adds native LAN video and a Video transport diagnostic.
LAN-only is the default and the explicitly selected policy on the test Yellow.
See [native LAN evidence](validation/ha-lan-native.json). The original relay
transport remains an explicit option; no silent cloud-media fallback is enabled.


The deployment has created the standalone camera, produced live HLS video and
validated local recordings, survived integration reload and HA restart, recovered
from a targeted media-socket shutdown, shared one relay session between two HLS
viewers and a 60-second recording, and cleaned up a recording cancelled by reload.
See [evidence](VIDEO_KEYPAD.md) for measured results and follow-up work. Native motion delivery and cloud-clip retrieval
remain unverified.

To return to the original bridge, disable the new MyQ config entry in Devices &
services, allow its stream to close, start the retained bridge add-on, and re-enable
the old bridge integration and generic camera entries. Enable add-on startup only
if you want that fallback to persist after a host restart. The bridge
retains its separate authorization. Do not copy HA's tokens back into a running
bridge. To restore an earlier native integration version, disable the entry,
restore the backed-up component directory, restart HA, and only enable the entry
if that version supports the configured devices. Avoid HACS updates from upstream
overwriting this experimental branch during validation.

## Daily use

The keypad device has cached Battery, Reported power state and Battery sample time
sensors, plus session state, active consumers, first-frame startup time, credential
renewals, current session problem, historical session-error count, recording status,
last recording and snapshot timestamps. Power samples survive reload/restart but
expire after 12 hours. Reading or updating these sensors never requests camera
power data. Explicit Refresh battery and power reads the cloud and coalesces repeat
requests within one minute; live startup reuses power reports already received.
The cached power state is a historical report, not proof the keypad is currently awake.
Session counters reset when the integration reloads.

Find clips in **Media → Local media → myq-keypad**. The Last recording entity also
exposes a `media_content_id` for automation playback. That timestamp records the
last completed recording; it does not guarantee the file survives retention or
manual deletion. Configure duration/cooldown/retention without signing in again.
Keep a dashboard's camera in `camera_view: auto` and preload disabled so simply
opening the dashboard does not maintain a live stream. Tap the camera for live view.

For event recording, call `myq.record_keypad` from a trusted HA trigger. Use an
automation with `mode: single`; the integration rejects overlapping recordings
and triggers inside the cooldown. Native myQ motion remains unverified; local
recording does not require a video subscription but has no pre-event buffer.
