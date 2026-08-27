# YoLink Local v0.7.2

Maintenance release for post-restart entity availability and platform
registration robustness.

## Fixes

- Ensures the Home Assistant `light` platform is registered in `PLATFORMS`.
- Adds a regression test that fails if an implemented entity platform is omitted
  from `PLATFORMS`.
- A transient YoLink `000201` during startup bootstrap now queues one immediate
  targeted verification after the bootstrap batch.
- The startup retry uses the existing serialized device-I/O worker.
- The old repeating all-device five-minute `getState` sweep remains removed.
- Retains the v0.7.1 YS6614 active-power telemetry repair.
- Retains YS5708 local relay control and four auxiliary-button triggers.
- Retains YS5707 local on/off and brightness support.
- Retains the HACS/Hassfest housekeeping fixes and brand assets.

## Why

A clean HACS reinstall/restart exposed two startup failure modes: an entity
platform can remain in the entity registry but show `Unavailable` if its
platform is not forwarded, and one transient startup `000201` can leave a device
with no liveness evidence until the periodic health loop retries it.

v0.7.2 adds guards for both cases.
