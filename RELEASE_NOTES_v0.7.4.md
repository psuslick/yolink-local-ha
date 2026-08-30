# YoLink Local v0.7.4

## Device-name diagnostics

v0.7.4 adds read-only diagnostics to isolate YoLink Local device-name
synchronization problems.

For every discovered device, Download Diagnostics now reports:

- `local_api_name` — the name parsed from the Local Hub `Home.getDeviceList` result;
- `ha_registry_name` — Home Assistant's integration-provided device-registry name;
- `ha_name_by_user` — any explicit Home Assistant user rename;
- `names_match` — whether the Local API and HA integration-provided names agree;
- the YoLink device ID, type, and model for unambiguous matching.

No entity IDs, device names, registry names, or user overrides are changed by this
diagnostic feature. It is read-only.

v0.7.4 retains the v0.7.3 device-name synchronization behavior and all v0.7.2
MQTT-first availability / targeted power-refresh behavior.
