# Native LAN video

Video Keypads can send encrypted H.264 directly to Home Assistant over two LAN
UDP channels (RTP and RTCP). No phone, Android runtime, subscription, or hardware
modification is required for this receiver path. Authorization, wake, device
metadata/session keys, signaling and UDP rendezvous still require vendor cloud
services. This is **local video transport**, not fully offline operation; it does
not prevent independent vendor uploads by the camera.

## Transport policy

Choose under **Settings → Devices & services → myQ → Configure**:

| Policy | Behavior |
| --- | --- |
| `lan_only` (default) | Only the camera's verified RFC1918 IPv4 candidate is used for media. Failure ends the attempt; no cloud media socket opens. |
| `lan_preferred` | Attempts LAN first; a startup failure explicitly permits cloud relay. A later LAN interruption starts a new attempt through the normal consumer recovery path. |
| `cloud_relay` | Uses the existing mode-0 TLS relay directly. |

Changing policy reloads the integration and closes its current sessions. OAuth
token rotation does not trigger this reload. The **Video transport** diagnostic
shows `connecting`, `lan`, `cloud_relay`, or `idle`; its attributes expose configured
policy, last transport and whether the current/latest attempt fell back to relay.
The policy currently applies to Video Keypads, not other myQ camera types.

HA needs a route to the camera's private IPv4 address and bidirectional UDP. The
camera chooses fresh ports during negotiation; a fixed RTSP port is not used.
There is no public-candidate or mode-11 server-relay selection in the LAN path.
A private address learned through authenticated metadata must match the rendezvous
candidate. Received media must match the selected peer IP and port; the AES key
is selected for the exact camera identity and stays in session memory.

## Reliability and battery

The receiver bounds packet sizes, fragment counts, outstanding sequences and total
buffer memory. It reorders and deduplicates fragments for up to 500 ms. A missing
fragment or sequence discards the damaged group of pictures and resumes at the next
complete IDR keyframe. Dependent frames are withheld during recovery. Conflicting
fragments, malformed packets, source changes, or a 15-second video stall end the
session and allow the consumer to reconnect. This version does **not** implement
proprietary packet retransmission. Recordings validate the finished file before
publication; a recording can span a gap and take longer than its media duration.
Live playback can pause during recovery. The transport diagnostic includes a
`lan_keyframe_recoveries` counter, and session state reports `recovering_lan`.

RTCP receiver reports and UDP keepalives run while a session is active. Viewers
and recordings share one camera session; the existing idle shutdown, recording
limits, cancellation, cached preview and 30-day retention settings remain in use.
There is no background video capture. Local transport is not evidence of lower
battery consumption: encoding, Wi-Fi transmission and wake time still dominate,
and recovery may increase wake time. Continuous viewing remains expensive.

## Validation and limits

See [feasibility evidence](LAN_P2P_RESEARCH.md) and the native deployment evidence
in `validation/ha-lan-native.json`. Tests cover packet bounds, decryption, reorder,
duplicates, loss, source selection, RTCP, resource cleanup, transport policy and
policy reload behavior. Hardware validation is on one keypad and HA Yellow;
other firmware/network combinations and extended battery life remain unmeasured.
Startup handshake retries occurred during validation, so startup time is variable.
This is an experimental deployment, not a reliability or battery-life certification.
See the [upstream convergence plan](CONTRIBUTION_PLAN.md): upstream already has a
LAN camera transport, and PR #11 adds standalone keypad support.

For troubleshooting, check **Session errors** (`last_failure`) and **Video
transport**. LAN-only errors should be fixed at the LAN path or explicitly changed
to another policy; the integration must never silently relax this privacy choice.

The final 60-second recording decoded all 1,200 frames while two HLS viewers
shared the session. A separate cancellation/playback run completed and closed
both LAN channels, but accumulated **24 keyframe recovery episodes** and delivered
fewer frames during its 65-second view. Recovery protects decoding; it does not
make this a lossless or production-ready stream. A startup handshake also needed
a retry. These are material follow-up issues for upstream comparison and tuning.
