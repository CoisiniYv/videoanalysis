# API Algorithm Rule Design

R3 adds API contracts for algorithm discovery and per-camera rule management.

## Registry

```http
GET /api/v1/algorithms
GET /api/v1/algorithms/{algorithm_type}
```

Each registry entry returns:

```json
{
  "algorithm_type": "intrusion",
  "display_name": "Intrusion",
  "category": "behavior_rule",
  "input_requirements": ["person_bbox", "track_id", "roi_polygon"],
  "supports_roi": true,
  "supports_line": false,
  "default_config": {
    "min_inside_ms": 1000,
    "cooldown_s": 30
  },
  "evidence_policy": {
    "snapshot_required": true,
    "clip_required": true,
    "pre_seconds": 5,
    "post_seconds": 10
  },
  "enabled": true
}
```

## Rule API

```http
POST /api/v1/cameras/{camera_id}/algorithm-rules
GET /api/v1/cameras/{camera_id}/algorithm-rules
PUT /api/v1/cameras/{camera_id}/algorithm-rules/{rule_id}
POST /api/v1/cameras/{camera_id}/algorithm-rules/{rule_id}/enable
POST /api/v1/cameras/{camera_id}/algorithm-rules/{rule_id}/disable
```

Create request:

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

Validation rules:

- `algorithm_type` must exist in the registry.
- `zone_id` and `line_id` must belong to the camera.
- `zone_id` is accepted only for algorithms that support ROI.
- `line_id` is accepted only for algorithms that support line input.
- `config` is merged with the registry default and validated against the
  algorithm schema.
- `evidence_policy` uses the same snapshot/clip schema for every algorithm.

## Runtime Export

The camera runtime config remains camera-centric:

```text
camera
  -> zones / lines
  -> algorithm rules
  -> exported cameras.yml-compatible runtime config
```

Savant continues to receive a camera config that resolves ROI and rule settings.
R3 does not edit `modules/savant_security/module.yml`.

## Scope

This is a contract/API phase. It does not implement new algorithm internals and
does not run performance tests.
