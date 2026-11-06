# LAN video feasibility — 2026-11-06

**Proven on this keypad and HA Yellow: video can travel directly over the LAN
without modifying hardware or using an Android/iOS app at runtime.** The installed
HA integration still uses its validated cloud relay; this was an isolated probe.

## Evidence

Two separately negotiated mode-9 sessions completed both RTP and RTCP peer
handshakes. The private peer IP matched the keypad's authenticated device metadata;
the peer ports changed between sessions, so the probe did not assume a fixed port.
It contacted only that verified private peer for the media plane. It opened no
cloud media socket and had no cloud media fallback.

| Session | LAN media datagrams | Complete decrypted access units | Fully decoded frames | Resolution |
| --- | ---: | ---: | ---: | --- |
| First | 427 | 111 | 111 | 1152×864 H.264 |
| Repeat | 552 | 104 | 104 | 1152×864 H.264 |

Both captures passed full FFmpeg decoding with no decode or decryption errors.
Media collection was bounded to six seconds after the start command, inside an
18-second UDP deadline. These are feasibility samples, not endurance results.
[Sanitized machine-readable evidence](validation/ha-lan-p2p-feasibility.json).

## Correct transport interpretation

The shipped application's Java and ARM64 client distinguish these transports:

- Mode 0: TCP cloud relay, used by the installed integration.
- Mode 2: proprietary direct TCP; the advertised port 9494 timed out while asleep
  and actively refused connections after a cloud wake. This path was not usable.
- Modes 8/9: RTP P2P, with mode 9 used for the encrypted-media proof.
- Modes 10/11: RTP **server relay**. Earlier references to mode 11 as evidence of
  direct P2P were incorrect. The wrapper name alone does not establish routing.

The camera advertises RTP support. CXNet returns a UDP rendezvous endpoint in the
raw options field of action 438. The native SDNK exchange registers the viewer's
RTP and RTCP endpoints and returns the camera's private/public candidates. The
probe acknowledges discovery, completes request/response/ack peer handshakes only
on the verified private candidate, and then sends the mode-9 video-start command.

Received packets use an eight-byte fragmentation header followed by RTP on fragment
zero. Reassembled `w1` packets use the existing AES-CBC media decoder and contain
length-prefixed H.264 access units. The session key is obtained through authenticated
cloud signaling and is never included in published evidence.

## What remains cloud-dependent

The successful path is:

`cloud authorization + wake + rendezvous → keypad ⇄ LAN ⇄ HA`

The live video leg is local. Authentication, device metadata/session key retrieval,
wake-up and rendezvous still use cloud services. There was no WAN-blocked test.
This does not establish fully offline operation or prove that the camera makes no
independent vendor uploads for other features.

The probes do not refresh credentials: they read HA's current access token in
memory and refuse to proceed near expiry. HA remains the sole refresh owner.
No router/firewall changes, firmware writes, camera-setting changes, or production
integration modifications were made. Probe sockets close and video-stop/logout
commands are sent during cleanup. The native HA entry remained loaded and idle.

## Next implementation milestone

1. Add a separate LAN session implementation behind the existing shared stream
   interface; retain the proven relay implementation.
2. Explicit transport policy: **LAN only** must fail clearly if local negotiation
   fails. Any cloud-relay fallback must be opt-in and visible in diagnostics.
3. Validate rendezvous candidates against device identity and permitted LAN routes;
   bound UDP packet sizes, fragment counts, buffering, retries and session lifetime.
4. Implement RTCP/keepalive, loss/reordering handling and bounded retransmission;
   test malformed packets, cancellation, key changes, reconnect and port changes.
5. Repeat HA playback/recording/concurrent-viewer/reload tests and longer sessions;
   measure startup, wake duration and battery separately. Local routing alone is
   not evidence of battery savings.

A separate later experiment can distinguish cloud control dependence from camera
uploads using device-scoped packet observation and narrowly controlled egress
restrictions. It is not required to establish this local receiver path.
