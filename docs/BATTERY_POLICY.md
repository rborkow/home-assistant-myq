# Battery and diagnostic scope

The native camera defaults to on-demand use. Cached snapshot requests do not wake
it; viewing and recording share one relay session. The session closes 20 seconds
after its final consumer leaves. HA's HLS producer has its own idle timeout, so
closing a dashboard does not start that 20-second countdown immediately. Keep
stream preloading disabled. The Yellow checks observed the relay close after idle
and remain closed after a cached snapshot request.

## What belongs in the integration

- Shared sessions, idle shutdown, bounded startup/retry and cancellation belong
  with the native camera. These are implemented and tested to the extent described
  in the validation report.
- Bounded recording belongs with the same session owner. Successful recordings
  include received-media duration and elapsed duration; interruptions can extend
  elapsed time. Validated completion and cancellation events work with HA automations.
- Battery percentage, the cloud-reported power state and sample time are implemented in a
  persistent native diagnostic cache. Sensor reads only read that cache, expire
  samples after 12 hours, and never initiate a wake or a cloud call. Reuse metadata obtained
  during explicit operations where possible. Offer an explicit status-refresh
  action if needed rather than adding a frequent power polling loop.
- Preserve a distinction between current session health and historical failures.
  Expose safe stage/type metadata, never raw exception messages, identities or tokens.

The native Refresh battery and power button is an explicit cloud read. Repeated
requests within one minute reuse the sample. A local timer expires stale entities
even when account polling is disabled; it makes no cloud request. Live startup
also captures power reports from requests the session already needs. Saved preview
frames and power samples survive restart without a camera wake.

## What stays in companion tooling

Keep battery baselines, sleep-tail experiments, repeated endurance recordings and
protocol traces outside the integration. These are deliberate measurement jobs,
not background features to enable on installation. Do not silently change camera
quality, motion sensitivity or recording schedules to pursue a battery target.

The earlier bridge's quiet 24-hour observation had five sparse cloud samples,
87% to 86% reported battery, zero observed bridge activity and no awake cloud
samples. Percentage rounding, cloud lag, motion and other clients make this too
weak to predict battery life or attribute savings to this fork. It is a baseline
observation, not an electrical measurement or a controlled comparison.

## Recording choices

| Policy | Benefit | Cost/limitation |
| --- | --- | --- |
| Explicit live view and short recordings | Few intentional wake sessions | Wake latency; no footage before the request |
| Recording from trusted HA triggers | Useful local clips without a cloud video subscription | Trigger delivery and wake latency can miss the start; cooldown and duration limits matter |
| Continuous live capture | Coverage without waiting for each trigger | Keeps the camera active; unsuitable as the battery default |
| Entitled cloud clips | May reuse recordings myQ already made | Requires entitlement and an API path that remains unverified here |

Native myQ motion delivery is still unverified. Do not advertise motion recording
until real event receipt, duplication handling, reconnect behavior and trigger
latency are measured. Registering a receiver is insufficient evidence.
