# YoLink Local v0.7.3

## Device-name synchronization

v0.7.3 makes the YoLink Local Hub the source of truth for integration-provided
Home Assistant device names.

- Device discovery now detects metadata changes even when the device-ID set is unchanged.
- Existing `Device` objects are refreshed in place so already-created Home Assistant
  entities immediately reference the current YoLink name and metadata.
- Existing Home Assistant device-registry entries have their integration-provided
  `name` synchronized from the Local Hub on startup/reload and discovery refresh.
- Explicit Home Assistant `name_by_user` overrides are preserved.
- Entity IDs are intentionally not renamed automatically; use Home Assistant's own
  entity-ID recreation/rename workflow when desired.
- Metadata-only updates do not recreate entities or fire add/remove platform handling.

This release preserves the v0.7.2 MQTT-first availability and targeted power-refresh behavior.
