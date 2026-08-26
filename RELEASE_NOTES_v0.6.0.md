# YoLink Local v0.6.0 — reliability redesign

This release changes availability handling from periodic all-device polling to an MQTT-first, targeted-verification model.

## Why

Real-world diagnostics showed multiple YS6614 outlets being marked unavailable on the integration's repeating five-minute HTTP refresh cadence. A later direct Local Hub `getState` probe could succeed while Home Assistant still showed the same device unavailable. Other probes returned transient YoLink error `000201` and the devices recovered seconds later. A single synchronous request failure is therefore not reliable evidence that a device is offline.

## Availability / transport changes

1. MQTT is the normal authoritative state path.
2. The repeating five-minute all-device `getState` fan-out is removed.
3. Liveness is tracked per device independently from raw `online` hints.
4. Line-powered devices can learn a stable MQTT cadence and use an adaptive stale threshold; battery/event-driven devices retain a conservative 12-hour threshold.
5. HTTP `getState` is used only for startup bootstrap, stale/suspect verification, new devices, command ambiguity, and command confirmation when MQTT does not arrive.
6. Runtime device I/O is serialized and targeted verification is jittered/de-duplicated.
7. `000201` is treated as a transient device-query failure. It never causes immediate `online:false`.
8. Hub/API transport health, device health, and command failure are tracked separately.
9. Any valid non-duplicate MQTT device report immediately restores device availability.
10. Home Assistant Download Diagnostics now exposes availability reason, liveness age, learned threshold, verification counts, error code/source, and Local transport health.

## Additional hardening after watchdog evidence

- A successful `getState` is positive liveness even when the response omits an `online` field or carries a contradictory raw hint.
- Device entities remain available in internal `suspect` state. They become unavailable only after stale liveness plus three spaced device-specific `000201` verification failures.
- HTTP transport failures do not count as device failures.
- The Local path remains usable if either MQTT or HTTP/API transport is still healthy; all entities are not failed merely because one transport is degraded.
- Successful commands no longer trigger an immediate `getState` on every operation. MQTT gets a short opportunity to confirm the state first; one targeted read is queued only if needed.
- A failed command does not overwrite the cached state with `online:false`.
- Ambiguous transport failures verify state before a command is reissued, avoiding accidental duplicate physical commands.
- No new database/file logger is added; health/cadence state is kept in memory.

## Compatibility

- Integration domain remains `yolocal`.
- Existing config entries, device identifiers, and entity unique IDs are preserved.
- Existing device/platform support from the madbrain76 v0.5.6 base is retained.
- Dual-host HTTP/MQTT support is retained.

This is a substantial coordinator change and should be tested with the independent YoLink Watchdog enabled during initial deployment.
- Offline-only MQTT hints are not treated as positive liveness unless they carry a newer device report timestamp or meaningful state payload.
- Rapid repeated commands coalesce confirmation work so an older command cannot trigger a stale follow-up read after a newer command.
