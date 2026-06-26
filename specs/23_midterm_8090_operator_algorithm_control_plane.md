# 23_midterm_8090_operator_algorithm_control_plane.md

Date: 2026-06-25

## 1. Purpose

This spec records the required direction for the midterm operator workflow:

```text
8090 is the single operator control plane.
```

Operators should use only the 8090 portal for:

- camera add/edit/enable/disable;
- zone and line configuration;
- algorithm enable/disable and per-camera rule parameters;
- people and face registration;
- watchlist and live-search management;
- evidence review, deletion, and storage maintenance;
- controlled runtime apply/restart/recovery.

This document is both the execution plan and the current control-plane record.
Implementation notes below mark which parts have already landed.

## 2. Current Finding

The 2026-06-25 runtime audit found that the current `lab` camera is not
configured with a per-camera watchlist rule.

Observed state:

- `lab` is enabled and mapped to
  `source_00000000-0000-4000-8000-781078565686`;
- `/api/v1/cameras/{lab_camera_id}/config` returns only
  `lab_intrusion_rule`;
- `camera_rules` contains only the primary and lab intrusion rules;
- generated `modules/savant_security/config/cameras.midterm.yml` exports only
  `behavior.intrusion` for `lab`;
- the event table has `lab` intrusion events, but no `lab` `watchlist_hit`
  events;
- face observations for `lab` exist, so the face detection/observation path is
  not completely absent.

At the same time, watchlist matching is globally enabled in `face-worker` by
compose/env:

```text
WATCHLIST_MATCH_ENABLED=true
WATCHLIST_THRESHOLD=0.60
WATCHLIST_TARGET_EXTERNAL_PERSON_IDS=demo:midterm:reese,demo:midterm:finch
WATCHLIST_TARGET_NAMES=Reese,Finch
```

This meant a `watchlist_hit` could exist in runtime data without proving that
the 8090 per-camera `face.watchlist` rule was active.

Implementation update on 2026-06-25:

- 8090 now stores per-camera `face.watchlist` targets in `camera_rules.config`;
- the saved fields are `target_person_ids`, `target_external_person_ids`, and
  `target_names`;
- `face-worker` now resolves enabled `face.watchlist` camera rules by
  observation `camera_id`;
- a configured rule with an empty target list emits no hits and does not fall
  back to "all people";
- env targets remain only as fallback for cameras with no per-camera rule.

## 3. Target Contract

The target contract is:

```text
operator input on 8090
  -> database state
  -> generated/runtime config
  -> runtime worker behavior
  -> persisted events/evidence
  -> visible 8090 status
```

If a control is visible as an operator switch, the UI must make one of these
states explicit:

- `production_ready`: the switch controls real runtime detection and evidence;
- `event_only`: the switch controls detection/event output, but evidence is not
  enabled by default;
- `config_only`: the value is saved/exported but does not affect runtime yet;
- `unsupported`: the algorithm is not implemented and cannot be enabled;
- `deferred`: the product contract exists, but implementation is intentionally
  postponed.

The UI must not let "saved to DB" be mistaken for "runtime active".

## 4. Support Matrix Requirement

Add an API support matrix that is the shared source of truth for 8090.

Minimum fields per algorithm:

```json
{
  "algorithm_id": "face.watchlist",
  "display_name": "名单命中",
  "category": "face",
  "configurable": true,
  "per_camera_gate": true,
  "runtime_detecting": true,
  "event_enabled": true,
  "evidence_enabled": true,
  "production_ready": true,
  "status": "production_ready",
  "status_reason": "face-worker resolves enabled per-camera face.watchlist rules from camera_rules",
  "requires_runtime_apply": true
}
```

The matrix must cover at least:

- `behavior.intrusion`
- `behavior.crowd_gathering`
- `behavior.fall`
- `behavior.chasing`
- `behavior.loitering`
- `behavior.running`
- `behavior.wall_climb_suspicious`
- `face.observation`
- `face.watchlist`
- `face.live_search`

8090 should render algorithm controls from this matrix and block or clearly mark
controls that are not production-ready.

## 5. Phase Plan

### P0 - Clarify Current UI Semantics

Goal: prevent operators from believing every visible algorithm button is fully
active.

Implementation requirements:

- add `/api/v1/algorithms/support-matrix` or extend the existing algorithm API
  with support-state fields;
- update 8090 algorithm cards to show support state and reason;
- disable save/apply for `unsupported` algorithms unless an explicit debug mode
  is enabled;
- label `face.watchlist` as a per-camera runtime gate with target-person
  selection;
- label `face.live_search` as deferred until the runtime path exists;
- keep `behavior.intrusion` marked as the current production-ready baseline.

Acceptance:

- 8090 shows a visible distinction between production-ready, partial, and
  config-only algorithms;
- static tests assert that unsupported/partial states are rendered;
- saving a rule no longer implies runtime readiness in UI text.

### P1 - Make Runtime Apply Report What It Applied

Goal: after an operator changes camera/rule state, 8090 must show whether the
runtime consumed it.

Implementation requirements:

- runtime apply/restart response includes generated runtime epoch, cameras,
  source ids, enabled rules, skipped rules, and unsupported rules;
- generated `cameras.midterm.yml` can be inspected through 8090 for the selected
  camera;
- operator UI shows the latest apply result and warnings;
- failed apply must leave 8090 online and report the actionable failure.

Acceptance:

- after changing a rule in 8090, the page can show whether that rule is in the
  generated runtime config;
- if a rule is saved but skipped by runtime, the operator sees the skip reason;
- runtime apply/restart keeps the 8090 management plane available.

### P2 - Close Behavior Algorithm Parity

Goal: make behavior algorithms have explicit production status instead of mixed
implicit behavior.

Implementation requirements:

- keep `behavior.intrusion` as the baseline production-ready path;
- for `behavior.crowd_gathering`, `behavior.fall`, and `behavior.chasing`,
  decide whether each should be `event_only` or `production_ready`;
- if production-ready, event-worker recording policy must create pending
  evidence tasks and record requests for those event types;
- implement and register missing rule modules before marking these as active:
  `behavior.loitering`, `behavior.running`, and
  `behavior.wall_climb_suspicious`;
- ensure every behavior event includes `camera_id`, `source_id`, `frame_uuid`,
  `keyframe_uuid`, `runtime_epoch_id`, `algorithm_type`, `rule_id`, `zone_id`
  or `line_id`, and `evidence_policy`.

Acceptance:

- each behavior algorithm has a support matrix state backed by tests;
- production-ready behavior algorithms can produce a real event and ready
  evidence bundle through 8090;
- unsupported behavior algorithms are not silently skipped without visible UI
  status.

### P3 - Make Face Algorithms DB-Driven

Goal: make face-related operator controls on 8090 authoritative.

Status: `face.watchlist` per-camera target membership is implemented.
`face.observation` remains pipeline/env controlled, and `face.live_search`
remains deferred.

Implementation requirements:

- `face.observation` per-camera enabled state gates face observation export for
  that camera, or the UI marks it as a pipeline-level control instead of a
  per-camera gate;
- `face.watchlist` rule config is read by face-worker from DB or from a runtime
  config generated from DB; **implemented from DB camera_rules**
- watchlist threshold, target set, camera scope, cooldown, and evidence policy
  come from 8090-managed state; **implemented for threshold, target set, and
  evidence policy**
- global env remains only a deployment default or emergency override, not the
  authoritative operator setting;
- face-worker logs and metrics report the active rule source:
  `source=db`, `source=runtime_config`, or `source=env_fallback`;
- `face.live_search` is either implemented end to end or hidden/deferred with a
  clear support matrix state.

Acceptance:

- enabling `face.watchlist` for one camera and disabling it for another changes
  runtime watchlist matching accordingly;
- a watchlist hit event can be traced back to the camera rule id and threshold
  configured on 8090;
- `lab` can be configured through 8090 to produce `watchlist_hit` events when a
  registered target appears and similarity exceeds the rule threshold;
- disabling the `lab` watchlist rule stops new `lab` watchlist events without
  disabling unrelated cameras.

### P4 - Unify Watchlist And People Management

Goal: make people, gallery, and watchlist target selection an operator workflow,
not an env-file edit.

Implementation requirements:

- 8090 can create/update people and gallery embeddings through the existing
  registration flow;
- 8090 can choose which people are watchlist targets; **implemented per camera**
- target membership is stored in DB and auditable; **implemented in
  camera_rules.config**
- face-worker refreshes target membership without requiring an image rebuild;
- stale/deleted people are not matched as active targets.

Acceptance:

- an operator can register a person, add that person to a watchlist target set,
  apply runtime, and receive a watchlist event/evidence without editing env;
- removing the person from the watchlist target set stops new hits after the
  configured refresh/apply boundary.

### P5 - Evidence And Storage Closure

Goal: make algorithm controls credible by proving their evidence path.

Implementation requirements:

- every production-ready alert algorithm has an evidence smoke;
- each smoke checks event row, evidence task, record request, Replay job,
  media-worker final bundle, raw clip range read, sidecar identity/annotation
  status, and 8090 display;
- evidence failures remain fail-closed with explicit reasons;
- 8090 evidence filters expose algorithm, camera, person, and state clearly.

Acceptance:

- `behavior.intrusion` remains passing after the control-plane changes;
- `face.watchlist` has at least one end-to-end smoke from 8090 config to ready
  evidence;
- unsupported or deferred algorithms do not create misleading empty evidence.

## 6. UI Requirements

8090 should be an operational workbench, not a JSON-only editor.

Required UI behavior:

- algorithm controls are grouped by behavior and face intelligence;
- each algorithm card shows support status and apply state;
- rule parameters use typed controls where practical;
- advanced JSON remains available for debug/internal use;
- save/apply buttons show whether a runtime restart/apply is required;
- errors show the exact API or runtime apply reason;
- camera display names are preferred over long `source_id` values, while
  source ids remain available for diagnostics.

## 7. API And Runtime Boundaries

The internal API service remains private to the compose network. Operators use
8090 only.

Allowed public operator routes:

```text
http://127.0.0.1:8090/operator
http://127.0.0.1:8090/api/v1/*
http://127.0.0.1:8090/api/bundles/*
```

Do not publish the internal API on host port 8000 as part of this work.

Runtime apply/restart may restart inference and worker services, but must not
restart or take down the 8090 management plane.

## 8. Validation Plan

Each implementation phase must end with targeted validation.

Minimum static checks:

```bash
python -m pytest \
  harness/tests/test_algorithm_rule_response_schema.py \
  harness/tests/test_runtime_config_export.py \
  harness/tests/test_camera_config_export.py \
  harness/tests/test_operator_face_registration_static.py \
  harness/tests/test_midterm_deployment_contract.py \
  -q

node --check services/evidence-viewer/app/static/operator.js
node --check services/evidence-viewer/app/static/evidence.js
docker compose -f infra/docker-compose.midterm.yml config
git diff --check
```

Minimum runtime checks when the midterm stack is available:

```bash
curl --noproxy '*' -fsS http://127.0.0.1:8090/health
curl --noproxy '*' -fsS http://127.0.0.1:8090/api/v1/cameras
curl --noproxy '*' -fsS http://127.0.0.1:8090/api/v1/algorithms
curl --noproxy '*' -fsS http://127.0.0.1:8090/api/v1/runtime/overview
```

For watchlist closure, add a dedicated smoke that proves:

```text
8090 config enables face.watchlist for lab
  -> runtime apply consumes the rule
  -> face-worker reports DB/runtime-config rule source
  -> lab face observation above threshold emits watchlist_hit
  -> event carries camera_id/source_id/rule_id/threshold
  -> evidence task and record request are created
  -> ready bundle appears in 8090
```

## 9. Non-Goals

This spec does not require:

- rebuilding the frontend as a new product shell;
- exposing port 8000 to operators;
- changing model weights or biometric embedding algorithms;
- making every declared algorithm production-ready in one batch;
- weakening evidence fail-closed guards to create fake passing bundles;
- bypassing runtime apply/restart when a controlled restart is required.

## 10. Handoff For Goal Execution

Use this spec as the starting contract for the follow-up goal. The first goal
implemented P0 and P1 before changing face-worker matching semantics.

Completed first goal boundary:

```text
Implement the 8090 algorithm support matrix and runtime apply visibility from
specs/23_midterm_8090_operator_algorithm_control_plane.md P0-P1. Preserve the
current intrusion evidence path and validate with targeted pytest, node checks,
compose config, and 8090 health.
```

Current second goal boundary:

```text
Implement DB/runtime-config driven face.watchlist controls from
specs/23_midterm_8090_operator_algorithm_control_plane.md P3-P4. Prove lab can
be enabled and disabled through 8090 as a per-camera watchlist source.
```
