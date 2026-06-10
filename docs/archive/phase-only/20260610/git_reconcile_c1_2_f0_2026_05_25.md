# Git Reconciliation — C1.2 and F0 Commit Order

**Date:** 2026-05-25

## 1. Original Problem

- `master` was at `87293e3` (C1.1 — camera config bridge).
- Two parallel work streams (C1.2 and F0) both landed on the same branch
  `f0-face-detection-readiness`, with C1.2 (`5c83854`) incorrectly stacked
  on top of F0 (`898f34c`).
- Desired order: C1.1 → C1.2 → F0.

## 2. Reconciliation Goal

Create a clean integration branch with the target commit order:

```
87293e3  C1.1  (master)
  -> C1.2  official adapter camera runtime control
  -> F0    face detection readiness harness
```

## 3. Backup Branches

| Branch | Purpose |
|--------|---------|
| `backup/f0-c1-2-mixed-before-merge` | Full `f0-face-detection-readiness` HEAD snapshot |
| `backup/master-before-c1-2-f0-merge` | `master` at `87293e3` before any merge |

## 4. Integration Branch

`integration/c1.2-f0-clean` (branched from `master` at `87293e3`)

## 5. Original → New Commit Mapping

| Original | New | Description |
|----------|-----|-------------|
| `5c83854` | `bb6c4b3` | feat(config): add official adapter camera runtime control |
| `898f34c` | `508dd96` | feat(face): add face detection readiness harness |

## 6. Cherry-pick Procedure

```
git checkout master
git checkout -b integration/c1.2-f0-clean
git cherry-pick 5c83854   # C1.2 — clean, no conflicts
git cherry-pick 898f34c   # F0   — clean, no conflicts
```

Zero conflicts — C1.2 and F0 have no file overlap.

## 7. Test Results

```
173 passed in 0.70s
  - 100 C1.2 tests (api_cameras, camera_config_export, camera_config_loader,
    config_export_script, runtime_config_export, camera_source_controller,
    behavior_rules_config_loader_integration, rule_registry, intrusion_rule_entrypoint)
  - 73 F0 tests (face_roi_selector, face_quality, scrfd_converter_utils,
    face_observation_event, face_config)
```

## 8. Current Smoke Status

- **C1.2 real video smoke:** NOT RUN (C1_TEST_SOURCE_URI not set)
- F0 has no runtime — no smoke applicable.

## 9. Next Steps

1. On operator host, set `C1_TEST_SOURCE_URI` and run:
   ```
   bash scripts/smoke/check_c1_2_official_adapter_runtime.sh
   ```
2. Smoke returns 0 → approve merge to master → enter F1.
3. Smoke fails → debug C1.2 runtime before F1.

## 10. Master Status

- `master` at `87293e3` — **not modified**.
- Awaiting user confirmation to fast-forward merge.
