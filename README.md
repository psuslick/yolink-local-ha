# YoLink Local

A Home Assistant custom integration that talks directly to the YoLink Local Hub over the LAN using the Local HTTP API and MQTT. This fork retains the device support and dual-host work from `madbrain76/yolink-local-ha` and changes the availability architecture to avoid false `unavailable` states caused by transient synchronous device-query failures.

## v0.6.0 reliability model

The normal state path is **MQTT-first**. There is no repeating five-minute `getState` request to every device. HTTP `getState` is reserved for startup bootstrap, newly discovered devices, stale/suspect verification, ambiguous command recovery, and command confirmation when MQTT does not arrive.

A YoLink `000201` / “Cannot connect to the device” response means that one synchronous hub-to-device request failed. It does **not** immediately make the Home Assistant device unavailable. Internally a device can be `available`, `suspect`, or `unavailable`; `suspect` remains usable in HA. A device becomes unavailable only after its liveness is stale and three spaced device-specific verification attempts have returned `000201`.

Any valid, non-duplicate MQTT report or successful `getState` immediately restores availability. A successful `getState` counts as positive liveness even if the hub response omits a raw `online` field.

For normally line-powered device types (`Outlet`, `Switch`, `Dimmer`, `MultiOutlet`), the integration can learn a stable MQTT cadence and choose an adaptive stale threshold, bounded between 30 minutes and 12 hours. Battery/event-driven devices use the conservative 12-hour threshold by default.

HTTP/API transport health, MQTT transport health, device health, and command failures are tracked separately. If one Local transport path fails while the other remains usable, device availability is not poisoned by the failed path.

See `RELEASE_NOTES_v0.6.0.md` for the full change list.

## Tested / inherited device support

The fork preserves the existing platform/device support from the v0.5.6 base, including DoorSensor, LeakSensor, Manipulator, MotionSensor, Outlet/YS6614, TempSensor, THSensor, TiltSensor, locks, switches, sirens, and valves supported by the upstream codebase. Existing entity unique IDs and the integration domain `yolocal` are unchanged.

## HACS installation

This repository is intended to be installed as a HACS custom integration.

1. In HACS, open **Custom repositories**.
2. Add `https://github.com/psuslick/yolink-local-ha` as category **Integration**.
3. Search for **YoLink Local** and choose **Download**.
4. Restart Home Assistant.
5. If the integration was already configured, keep the existing config entry; do not delete and recreate it.

For reliable HACS update detection, publish/tag the repository version that matches `custom_components/yolocal/manifest.json` (for this release, `v0.6.0`).

## Configuration prerequisites

- YoLink Local Hub with the relevant devices migrated to the Local Network.
- HTTP and MQTT enabled on the hub.
- Hub host/IP, Local API Client ID, Client Secret, and Net ID from the YoLink app.
- Optional secondary host/IP may be configured for the same physical Local Hub.

## Diagnostics

Home Assistant **Download diagnostics** for the YoLink Local config entry now includes redacted configuration plus compact runtime and per-device availability data, including:

- current internal availability status and reason;
- last MQTT / successful HTTP liveness;
- liveness age;
- adaptive stale threshold and its source;
- `000201` failure count;
- verification request/success/unreachable counters;
- last error code/source;
- MQTT and HTTP/API transport health.

Credentials and configured hub addresses are redacted from the config-entry portion of diagnostics.

## Storage / write behavior

The availability and learned-cadence state is kept in memory. This integration does not add a custom database, event journal, or high-frequency file logger. Normal Home Assistant Recorder behavior still applies to entity state changes.

## Credits

This fork builds on the work of David Bruce Borenstein (`borenstein/yolink-local-ha`) and `madbrain76/yolink-local-ha`. Availability design also benefited from comparing other public forks and from independent YoLink Watchdog observations of Local Hub behavior.

## License

GNU General Public License v3.0. The existing repository `LICENSE` remains authoritative.
