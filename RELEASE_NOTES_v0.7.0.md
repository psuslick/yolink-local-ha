# YoLink Local v0.7.0

v0.7.0 turns the v0.6.1 YS5708/YS5707 capture work into native Home Assistant functionality while retaining the MQTT-first availability architecture introduced in v0.6.0.

## YS5708-UC in-wall switch

The Local Hub capture from a real YS5708-UC confirmed four distinct `Switch.DevEvent` payloads:

- Button 1 short: `keyMask: 1`, `type: Press`
- Button 1 long: `keyMask: 1`, `type: LongPress`
- Button 2 short: `keyMask: 2`, `type: Press`
- Button 2 long: `keyMask: 2`, `type: LongPress`

v0.7.0 therefore adds:

- a native Home Assistant `switch` entity for YoLink `Switch` devices, including YS5708-UC;
- four native device automation triggers for validated YS5708 models;
- a `yolocal_event` event-bus event for Local Hub `*.DevEvent` messages, including the HA device ID, YoLink source device ID, model, event name, button number and press type when recognized.

Relay/state messages remain separate from physical-button messages, so automations do not need to infer button presses from the fan relay changing state.

## YS5707-UC dimmer

Adds a native Home Assistant `light` entity for YoLink `Dimmer` devices. The YS5707 capture confirmed Local Hub state uses `state: open/closed` plus a 0–100 `brightness` value.

The new light platform provides:

- on/off control;
- Home Assistant 0–255 brightness mapped to YoLink 0–100;
- MQTT-first state updates and the same command-confirmation/availability logic as the rest of the integration.

Turning a light off does not deliberately overwrite remembered brightness.

## Availability / Local Hub reliability

The v0.6.x architecture remains unchanged:

- MQTT is the normal state path;
- no repeating five-minute all-device `getState` fan-out;
- transient `000201` is not immediate proof of device unavailability;
- stale/suspect devices are verified with serialized, targeted reads;
- MQTT, HTTP/API, device health and command failures remain separate concepts.

## Diagnostics

The bounded RAM-only MQTT event capture remains available in Download Diagnostics. It is still useful for future unsupported-device reverse engineering and does not create custom Recorder/file/database writes.

## HACS / repository housekeeping

This release also addresses the file-based failures seen in the v0.6.1 GitHub validation run:

- removes the unsupported `icon` key from `manifest.json`;
- adds local integration brand assets under `custom_components/yolocal/brand/`;
- expands CI from only availability tests to the complete regression suite;
- updates checkout/setup-python actions and opts the workflow into Node.js 24.

Two HACS checks are GitHub repository settings and therefore cannot be fixed by the package itself: **Issues must be enabled** and **repository topics must be configured**. See `REPOSITORY_SETTINGS.md`.
