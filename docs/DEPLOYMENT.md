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
they do not wake the keypad, can be stale, and are unavailable until the first
live keyframe after a restart. Live viewing still uses myQ/Tend cloud services.

## Recording

Call `myq.record_keypad` targeting the keypad camera, with `duration` between 5
and 300 seconds. Duration measures received video at the negotiated 20 fps. Wake-up
and bounded reconnection can extend elapsed time. No audio or sleeping-camera
pre-event buffer is provided.

Validated files appear under the configured local media directory's `myq-keypad`
folder. HA emits `myq_keypad_recording_finished` with the entity ID and status:

- `completed`: includes path, decoded frames, media seconds and wall seconds.
- `failed`: includes an error type without service credentials or raw responses.
- `cancelled`: recording was interrupted, for example during integration reload.

Only completed, validated clips are published. Partial files are removed on handled
failure/cancellation; abrupt host power loss may leave partial files. No automatic
retention deletes existing clips. New recordings are refused below 1 GB free space
or at 2 GB of stored MP4s; this is an admission check, not a hard quota.

## Yellow validation and rollback

The deployment has created the standalone camera, produced live HLS video and
validated local recordings, survived integration reload and HA restart, recovered
from a targeted media-socket shutdown, shared one relay session between two HLS
viewers and a 60-second recording, and cleaned up a recording cancelled by reload.
See [evidence](VIDEO_KEYPAD.md) for measured results and follow-up work. Native motion delivery and cloud-clip retrieval
remain unverified.

To return to the original bridge, disable the new MyQ config entry in Devices &
services, allow its stream to close, and use the existing bridge camera. The bridge
retains its separate authorization. Do not copy HA's tokens back into a running
bridge. To restore an earlier native integration version, disable the entry,
restore the backed-up component directory, restart HA, and only enable the entry
if that version supports the configured devices. Avoid HACS updates from upstream
overwriting this experimental branch during validation.
