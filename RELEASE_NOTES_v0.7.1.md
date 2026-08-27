# YoLink Local v0.7.1

v0.7.1 is a maintenance release for YS6614-UC Outlet active-power telemetry. It retains the v0.7.0 YS5708 button-trigger and YS5707 dimmer support and does not restore the old all-device five-minute polling loop.

## Fixed: Outlet power can remain unknown/stale

After the MQTT-first reliability redesign removed the old concurrent five-minute `getState` sweep, YS6614 relay availability remained healthy but active-power telemetry could be left unknown or stale. The Local Hub can publish sparse `Outlet.StatusChange` messages that contain relay state and alerts but no instantaneous `power` sample. If startup `getState` did not establish a power value, or if a later payload supplied a null/malformed `power`, Home Assistant could therefore have no current local measurement to display.

Removing the official YoLink cloud integration can make this more visible because its separate cloud current-power entity is no longer present; the Local integration does not depend on the cloud integration.

### v0.7.1 behavior

- Keeps `power` as the active instantaneous-power source. YoLink reports it in deciwatts, so the existing HA sensor continues to divide by 10.
- Does **not** treat the separate `watt` field as instantaneous-power fallback. Real Outlet payloads can report nonzero `power` while `watt` is zero.
- Preserves the last valid active-power sample across sparse MQTT state messages.
- Ignores null, malformed, NaN/Inf, negative, or boolean `power` values instead of letting them erase a valid cached sample.
- Treats an explicit relay-off (`closed`) state as a known 0 W active-power condition.
- When an Outlet is ON and its active-power sample is missing or at least five minutes old, queues one targeted `getState` through the integration's existing serialized device-I/O path.
- Any valid MQTT `power` sample resets the power-freshness timer, so HTTP is not used when MQTT is already supplying current telemetry.
- A failed telemetry refresh, including transient `000201`, keeps the last valid power and does not count as an availability failure unless a separate health verification is also due.
- Failed telemetry refreshes are rate-limited to avoid a request loop.

This is intentionally much narrower than the pre-v0.6 design: only stale/missing power on an ON Outlet can cause a telemetry refresh. There is still no repeating all-device `getState` fan-out.

## Diagnostics

Download Diagnostics now includes an `outlet_power` section with RAM-only information for each Outlet:

- last valid power timestamp/age;
- raw deciwatt and converted watt values;
- refresh requests, successes, failures and `000201` counts;
- invalid incoming power-sample count;
- last refresh error.

YS6614 models are also included in the existing bounded 100-event RAM-only MQTT capture. No custom database, persistent telemetry journal, or high-frequency file logging is added.

## Regression coverage

New tests cover:

- `power: 445` = 44.5 W while `watt: 0` is ignored as an instantaneous-power fallback;
- sparse `Outlet.StatusChange` preserving prior power;
- null/malformed power not overwriting cache;
- relay-off forcing 0 W;
- refresh only for ON outlets with stale/missing power;
- immediate refresh eligibility after an outlet turns on;
- refresh-failure cooldown and state preservation;
- invalid samples not being counted as fresh measurements.

The full v0.7.1 regression suite contains 33 tests.

## Upgrade

Update through HACS, restart Home Assistant, and leave the existing YoLink Local config entry in place. Existing device identifiers and power-sensor unique IDs are unchanged.
