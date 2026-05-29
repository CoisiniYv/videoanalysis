# Operator: Configure ROI and Rules

Status: R2 operator draft.

## Purpose

Configure camera zones and behavior rules without editing the Savant pipeline. The runtime reads generated camera config through `CAMERAS_CONFIG_PATH`.

## Concepts

- `camera_id`: operator-facing camera identifier.
- `source_id`: Savant stream/source identifier.
- Zone: named polygon in frame coordinates.
- Rule: behavior logic that consumes tracks, pose data, and zones.
- Intrusion: current main behavior rule.

## Example Shape

```yaml
cameras:
  cam_front_gate:
    enabled: true
    source_id: front_gate_rtsp
    name: Front Gate
    zones:
      restricted:
        polygon:
          - [100, 120]
          - [600, 120]
          - [620, 520]
          - [90, 520]
    rules:
      intrusion_restricted:
        type: intrusion
        zone: restricted
        enabled: true
```

## Verification

1. Export runtime config.
2. Confirm `modules/savant_security/config/cameras.generated.yml` or the configured temporary file contains the camera, zone, and rule.
3. Start `infra/docker-compose.c1-official-adapter.yml`.
4. Check `c1-official-savant` logs for `CAMERAS_CONFIG_PATH`.
5. Confirm Redis `security.events` receives behavior events when tracks enter the ROI.
6. Confirm PostgreSQL event rows contain the expected `camera_id` and `source_id`.

## Future Rule Slots

- Loitering.
- Crowd density.
- Fall detection.
- Rule chaining with face/person identity after production watchlist design.

## Common Troubleshooting

- No events: polygon may not cover the tracked person center or rule may be disabled.
- Wrong camera: check `source_id` mapping.
- Config not loaded: check `CAMERAS_CONFIG_PATH` and container mount.
- Excess events: adjust cooldown and rule thresholds.

Do not edit `modules/savant_security/module.yml` for ordinary ROI or rule changes.

