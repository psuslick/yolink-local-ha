# YoLink Local

### v0.7.4 device-name diagnostics

v0.7.4 adds a read-only `device_names` diagnostics section showing the Local Hub
API name and Home Assistant registry name side-by-side for every YoLink device.


### v0.7.3 device-name synchronization

v0.7.3 synchronizes integration-provided Home Assistant device names with the current
name reported by the YoLink Local Hub. Existing entity objects are refreshed in place;
Home Assistant user name overrides and entity IDs are left untouched.


A Home Assistant custom integration that talks directly to the YoLink Local Hub over the LAN using the Local HTTP API and MQTT. This fork retains the broad device support from `madbrain76/yolink-local-ha` and replaces false-unavailable-prone polling with an MQTT-first availability model.


### v0.7.2 startup/platform recovery

v0.7.2 adds a regression guard ensuring implemented Home Assistant entity
platforms (including `light`) remain present in `PLATFORMS`. It also closes a
startup availability gap: a transient `000201` during the initial bootstrap is
queued for one immediate serialized targeted verification after the bootstrap
batch instead of waiting for the periodic health loop. Blanket polling is not
restored.


## v0.7.2 highlights

### YS6614 active-power telemetry

v0.7.2 fixes a telemetry gap exposed after the v0.6 MQTT-first redesign. YoLink Outlet MQTT `StatusChange` reports can be sparse and may carry relay state without an instantaneous `power` sample. The old five-minute all-device HTTP sweep incidentally refreshed power, while the redesigned integration correctly removed that sweep to eliminate false-unavailable clusters. As a result, an Outlet whose startup `getState` missed or whose later payload carried a null/malformed `power` could leave the Home Assistant power sensor unknown/stale even though the outlet itself remained healthy.

The fix keeps the MQTT-first availability design and adds a narrow telemetry path instead of restoring broad polling:

- valid `power` remains the authoritative instantaneous field and is converted from YoLink deciwatts to watts by the existing sensor;
- `watt` is **not** used as a fallback for instantaneous power;
- sparse Outlet MQTT messages preserve the last valid power sample;
- null/malformed `power` values no longer erase a valid cached reading;
- an explicit relay-off report sets active power to 0 W;
- only an **ON Outlet** with missing/stale active-power telemetry becomes eligible for one serialized targeted `getState`;
- active-power telemetry becomes stale after five minutes, but any valid MQTT power sample resets that timer and suppresses the HTTP refresh;
- targeted power-refresh `000201` failures do not by themselves change device availability or erase power state;
- power telemetry counters/timestamps are RAM-only and included in Download Diagnostics.

YS6614 events are also included in the existing bounded 100-event RAM-only diagnostic capture so future raw Local Hub payload behavior can be verified without file logging.

### v0.7.0 device support retained

### YS5708-UC in-wall switches

YS5708 devices are now exposed as native Home Assistant `switch` entities. The two auxiliary physical buttons are also exposed as native **device automation triggers** based on payloads confirmed from a real Local Hub:

- Button 1 short press
- Button 1 long press
- Button 2 short press
- Button 2 long press

The Local Hub reports these as `Switch.DevEvent` with `keyMask` 1/2 and `type` `Press`/`LongPress`. The integration also fires a compact `yolocal_event` event on the HA event bus for `*.DevEvent` messages so advanced automations can use the raw device-event context without creating fake button entities.

### YS5707-UC dimmer

YoLink `Dimmer` devices are exposed as native Home Assistant `light` entities with on/off and brightness control. Local Hub brightness is 0–100 and is translated to Home Assistant's 0–255 brightness scale.

See `RELEASE_NOTES_v0.7.2.md` for the power fix and `RELEASE_NOTES_v0.7.0.md` for the YS5708/YS5707 feature release.

## Reliability model

The normal state path is **MQTT-first**. There is no repeating five-minute `getState` request to every device. HTTP `getState` is reserved for startup bootstrap, newly discovered devices, stale/suspect verification, ambiguous command recovery, and command confirmation when MQTT does not arrive.

A YoLink `000201` / “Cannot connect to the device” response means that one synchronous hub-to-device request failed. It does **not** immediately make the Home Assistant device unavailable. Internally a device can be `available`, `suspect`, or `unavailable`; `suspect` remains usable in HA. A device becomes unavailable only after its liveness is stale and multiple spaced device-specific verification attempts fail.

Any valid, non-duplicate MQTT report or successful `getState` immediately restores availability. A successful `getState` counts as positive liveness even if the Hub response omits a raw `online` field.

For normally line-powered device types (`Outlet`, `Switch`, `Dimmer`, `MultiOutlet`), the integration can learn a stable MQTT cadence and choose an adaptive stale threshold, bounded between 30 minutes and 12 hours. Battery/event-driven devices use the conservative 12-hour threshold by default.

HTTP/API transport health, MQTT transport health, device health, and command failures are tracked separately.

## Tested / inherited device support

The fork preserves existing platform/device support from the v0.5.6 base, including DoorSensor, LeakSensor, Manipulator, MotionSensor, Outlet/YS6614, TempSensor, THSensor, TiltSensor, locks, sirens, and valves. v0.7.x additionally validates:

- **YS5708-UC** — local switch control plus four auxiliary-button triggers;
- **YS5707-UC** — local light on/off and brightness.

Existing integration domain `yolocal` and device identifiers are unchanged.

## HACS installation

1. In HACS, open **Custom repositories**.
2. Add `https://github.com/psuslick/yolink-local-ha` as category **Integration**.
3. Search for **YoLink Local** and choose **Download**.
4. Restart Home Assistant.
5. If the integration was already configured, keep the existing config entry; do not delete and recreate it.

For reliable update detection, publish a GitHub Release matching `custom_components/yolocal/manifest.json` (for this release, `v0.7.4`).

## Configuration prerequisites

- YoLink Local Hub with the relevant devices migrated to the Local Network.
- HTTP and MQTT enabled on the Hub.
- Hub host/IP, Local API Client ID, Client Secret, and Net ID from the YoLink app.
- Optional secondary host/IP may be configured for the same physical Local Hub.

## YS5708 automation triggers

In the Home Assistant automation UI, choose **Device** as the trigger and select the local YS5708 device. The four physical-button trigger choices should appear directly.

For advanced event-bus use, listen for `yolocal_event`. Recognized YS5708 button events contain fields including `device_id`, `source_device_id`, `type`, `button`, `key_mask`, `press_type`, `model`, and `event_name`.

## Diagnostics

Home Assistant **Download diagnostics** includes redacted configuration plus compact runtime/per-device availability and Outlet active-power telemetry information. The bounded RAM-only MQTT event capture remains available for future protocol/device investigation. It does not create a custom log, database, or Recorder entity and resets on Home Assistant restart.

## Storage / write behavior

Availability, learned cadence, Outlet power-telemetry freshness, and MQTT event capture state are kept in memory. This integration does not add a custom database, persistent event journal, or high-frequency file logger. Normal Home Assistant Recorder behavior still applies to entity state changes.

## Repository validation

See `REPOSITORY_SETTINGS.md` for HACS requirements that live in GitHub repository settings rather than files (notably Issues and repository topics).

## Credits

This fork builds on the work of David Bruce Borenstein (`borenstein/yolink-local-ha`) and `madbrain76/yolink-local-ha`. Availability design also benefited from comparing other public forks and from independent YoLink Watchdog observations of Local Hub behavior.

## License

GNU General Public License v3.0. The existing repository `LICENSE` remains authoritative.
