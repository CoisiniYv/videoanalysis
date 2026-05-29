# Operator: Add Algorithm Rule

R3 configures algorithm rules through the API. Do not edit
`modules/savant_security/module.yml` for camera rule changes.

## Algorithm List

Use `GET /api/v1/algorithms` to see the registry:

```text
intrusion
loitering
crowd_gathering
running
chasing
fall
wall_climb
face_intelligence
```

`face_intelligence` covers watchlist hits, trajectory / appearances queries,
and live search. These are not configured as three separate algorithm families.

## Camera, ROI, And Rule

The normal order is:

1. Create or confirm the camera.
2. Create zones or lines for the camera.
3. Create an algorithm rule that references `zone_id` or `line_id`.
4. Export runtime config for Savant.

Example rule:

```http
POST /api/v1/cameras/cam_001/algorithm-rules
```

```json
{
  "algorithm_type": "intrusion",
  "enabled": true,
  "zone_id": "perimeter",
  "line_id": null,
  "config": {
    "min_inside_ms": 1000,
    "cooldown_s": 30
  },
  "evidence_policy": {
    "snapshot_required": true,
    "clip_required": true,
    "pre_seconds": 5,
    "post_seconds": 10
  }
}
```

The API validates that `algorithm_type` exists in the registry, and that
`zone_id` / `line_id` belongs to the camera.

## Enable Or Disable

```http
POST /api/v1/cameras/cam_001/algorithm-rules/12/enable
POST /api/v1/cameras/cam_001/algorithm-rules/12/disable
```

## Limits

R3 only defines the configuration contract. It does not add new loitering,
running, chasing, fall, or wall-climb detector logic, and it is not a
performance test phase.
