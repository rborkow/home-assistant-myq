# MyQ for Home Assistant

[![CI](https://github.com/bvdcode/home-assistant-myq/actions/workflows/ci.yml/badge.svg)](https://github.com/bvdcode/home-assistant-myq/actions/workflows/ci.yml)
[![HACS](https://github.com/bvdcode/home-assistant-myq/actions/workflows/hacs.yml/badge.svg)](https://github.com/bvdcode/home-assistant-myq/actions/workflows/hacs.yml)
[![Hassfest](https://github.com/bvdcode/home-assistant-myq/actions/workflows/hassfest.yml/badge.svg)](https://github.com/bvdcode/home-assistant-myq/actions/workflows/hassfest.yml)
[![Support on Ko-fi](https://img.shields.io/badge/Support_on_Ko--fi-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/bvdcode)

MyQ connects Chamberlain and LiftMaster garage-door openers to Home Assistant
through the MyQ residential cloud service.

The integration provides:

- configuration entirely through the Home Assistant user interface;
- email or SMS verification during sign-in;
- automatic OAuth token refresh while authorization remains valid;
- discovery of garage doors exposed by the MyQ account;
- current door state and cloud availability;
- open and close controls;
- a door-open binary sensor for Alarmo and other sensor-based automations;
- vacation mode, work-light, fault, cycle-count, and activation diagnostics
  when provided by MyQ;
- snapshots from attached cameras and experimental live video for TC cameras;
- cloud polling every 30 seconds, temporarily more frequently after a command.

## Alarmo

Each garage door includes a **Door open** binary sensor with the `garage_door` device class. Add this sensor in Alarmo's **Sensors** tab. The existing cover provides the open and close controls.

The sensor is on whenever the door is known to be not closed, including while it is moving. Unknown states remain unknown, and offline or missing doors are unavailable. The sensor shares cloud polling with the cover.

After an accepted open or close command, the integration waits 1 second before checking the status. The intervals then increase to 2, 3, 4, and 5 seconds and remain at 5 seconds. Polling continues for at least 30 seconds, even if the requested state is reported earlier, and for up to 60 seconds while waiting for it. A new command restarts the sequence and polling window for that door. After an accepted command from a fully closed or open door, the cover immediately shows Opening or Closing. If the first status response still reports the original state, the cover keeps this provisional movement until the next response. A changed status replaces it immediately; from the second response onward, the cover always uses the state reported by MyQ. All sensors continue to use confirmed MyQ data. Polling returns to the normal 30-second interval when tracking finishes, the door becomes unavailable, or an update fails. If polling is disabled in Home Assistant system options, commands keep the last confirmed state without provisional movement or automatic status requests; manual updates still work.

## Requirements

- Home Assistant 2026.7.4 or newer;
- a MyQ residential account with at least one connected garage-door opener.

The initial release has been tested with a United States MyQ account. Reports
from other regions are welcome.

## Installation

The integration can be installed through HACS as a custom repository.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bvdcode&repository=home-assistant-myq&category=integration)

1. Open **HACS**.
2. Select **Integrations**.
3. Open the menu and select **Custom repositories**.
4. Add `https://github.com/bvdcode/home-assistant-myq` as an **Integration**
   repository.
5. Install **MyQ** and restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up MyQ.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=myq)

1. Open **Settings → Devices & services**.
2. Select **Add integration**.
3. Search for **MyQ**.
4. Choose automatic or browser sign-in.
5. For automatic sign-in, enter the email address and password used by the
   MyQ app, then select email or SMS delivery and enter the six-digit
   verification code.

Browser sign-in is always available and is also offered automatically if MyQ
blocks the automatic flow. Enter the account email address, complete
authentication on the MyQ website, and paste the resulting
`com.myqops://android` callback address into Home Assistant.

If the callback address does not appear, press **F12**, open the browser
console, and find the message that starts with
`Failed to launch 'com.myqops://android?` and says that the scheme does not
have a registered handler. Copy the complete `com.myqops://android?...`
address from that message and paste it into Home Assistant.

Browser sign-in completes the interactive login step. It does not bypass
restrictions on token exchange or device API access.

The password is used only during sign-in and is not stored. Home Assistant
stores the issued OAuth tokens in the config entry and refreshes them
automatically. If MyQ rejects token refresh or account access, Home Assistant
requests reauthentication.

## Cameras

Openers with an attached camera get a camera entity on the same device.
Snapshots are fetched on demand from the Tend cloud using the existing MyQ
authorization. Concurrent requests share an image request.

TC cameras also offer experimental live H.264 video. Home Assistant must be able
to reach the camera over the local network. Playback uses the `stream`
integration or Home Assistant's built-in go2rtc. An external go2rtc server on
another host or in another container cannot access the loopback video source.
Other camera generations currently provide snapshots only.

Viewers share one camera session, which closes 60 seconds after the last viewer
disconnects. Opening the camera simultaneously in the myQ app may interrupt the
session. Audio and retrieval of cloud recordings are not implemented.

The `camera_serial_prefix` attribute identifies the camera generation. Connection
failures are logged with `TC camera live video unavailable`. Real-camera playback
and recovery still require validation on this implementation.

## Safety

Opening or closing a garage door can cause injury or property damage. Keep the
door area clear and maintain the opener's photo eyes, obstruction detection,
and other safety equipment. Do not use remote control where unattended
operation is prohibited.

## Development

Use Python 3.14 or newer and install the test dependencies:

```bash
python -m pip install -r requirements_test.txt
ruff check .
ruff format --check .
mypy custom_components/myq tests
pytest
```

## Project status

This is a community-maintained project and is not affiliated with, endorsed by,
or associated with The Chamberlain Group LLC. MyQ, Chamberlain, and LiftMaster
are trademarks of their respective owners.

### Restrict camera video to the LAN

In **Settings → Devices & services → MyQ → Configure**, enable **Require LAN-only
camera video** to accept video only from the camera's RFC1918 IPv4 address reported
by authenticated signaling. The selected UDP peer's address and port are pinned;
public or different private candidates are not used. If that LAN path is missing
or unreachable, live playback fails instead of choosing a public candidate.

This option is off by default to preserve existing routing behavior. Changing it
reloads the integration and closes active camera sessions. Cloud authorization,
signaling and connection setup remain necessary, and this option does not prevent
independent uploads by the camera. It governs live video, not cloud snapshots.

### Video Keypad previews and battery

The Video Keypad preview is passive: opening an idle dashboard does not fetch a
cloud image or start live video. It shows the most recent keyframe received during
an explicit live session, saved locally and restored after integration reload or
Home Assistant restart. Before the first live view there is no preview. The
`preview_updated` camera attribute gives its capture time; the image may be stale.

Keypad video uses a 20-second idle grace period after the final internal consumer
leaves (Home Assistant's own stream worker may remain attached longer). Ordinary
garage camera behavior is unchanged. Avoid stream preloading on a battery keypad.
Live viewing still uses battery and cloud authorization/signaling; this change
does not add background recording or establish a battery-life improvement.
