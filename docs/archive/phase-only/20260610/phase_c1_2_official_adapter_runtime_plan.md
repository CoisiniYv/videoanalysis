# Phase C1.2 — Official Adapter Runtime Plan

Date: 2026-05-25
Status: **planning component of the C1.2 change set** (companion to
`docs/phase_c1_2_official_adapter_runtime.md`, which records the
implementation result).

This document captures the topology review that drove C1.2's design
decisions.

---

## 1. Savant's official adapter ↔ module separation

The Savant project ships an explicit boundary between **source adapters**
(processes that ingest external media — RTSP, HTTP, files, USB
cameras, Kafka, etc.) and **modules** (long-lived GPU pipelines that
do inference, tracking, and rule evaluation). The two halves
communicate over **ZeroMQ** using the Savant-RS frame protocol.

Key consequences:

- A module never speaks RTSP directly. It exposes a ZMQ socket
  (typically `router+bind:tcp://0.0.0.0:5555`) and waits.
- Each external source is a separate adapter process. Spawning or
  stopping an adapter = dynamically attaching or detaching a stream.
- Adapters are intentionally stateless and replaceable. The module
  survives an adapter restart, and an adapter survives a module
  restart (it will reconnect when the ZMQ socket comes back).
- Reference adapters are published at
  `ghcr.io/insight-platform/savant-adapters-gstreamer` for the common
  protocols. Retina RTSP Service is the recommended high-density RTSP
  ingestion path for production deployments.

This is the model C1.2 aligns to.

## 2. camera_id ↔ source_id mapping

The project uses two identifiers for "a camera":

| Identifier | Lives in | Meaning |
|---|---|---|
| `camera_id` | PostgreSQL `cameras.id`, API JSON, business logic | Human-meaningful site identifier (e.g. `cam_001`, `north_gate_4`) |
| `source_id` | Savant adapter / module / events | ZMQ frame-stream identifier (e.g. `phase3h`, `north_gate_4_rtsp`) |

The mapping is **1-1 and enforced by the DB**: `cameras.source_id` has
a `UNIQUE` constraint (C1, migration 004).

Rule: every layer that needs to address "the camera" picks the right
half of the pair.
- DB rows, API URLs, business queries → `camera_id`.
- Adapter container env, ZMQ frame metadata, `SecurityEvent.source_id`,
  `metadata.json` filenames → `source_id`.

`BehaviorRulesPyFunc` (C1.2) bridges them: it indexes per-source
runtime by `source_id`, but stamps emitted `SecurityEvent.camera_id`
with the configured business value.

## 3. Current phase3h-zmq gaps vs. the official model

`infra/docker-compose.phase3h-zmq.yml` is already half-official:

| Layer | Status today |
|---|---|
| Module ZMQ source | ✓ uses `zeromq_source_bin` + ROUTER bind |
| Module ZMQ sink | ✓ uses `pub+bind` for metadata + video file sinks |
| Source adapter | ✗ **inline in compose**, single hardcoded `SOURCE_ID=phase3h`, LOCATION pinned to `/testVideo/test.mp4` |
| Camera config schema | ✗ legacy `zones.<name>.polygon` / `rules.<name>.rule_type`, hand-edited |
| ROI / rule lifecycle | ✗ requires editing `modules/savant_phase3h_zmq/config/cameras.yml` and restarting savant |
| Adapter lifecycle | ✗ `docker compose up` brings everything up together; cannot attach/detach a single source |

The module/sink side is fine. The adapter side and the configuration
side are the gaps C1.2 closes.

## 4. C1.2 implementation route

C1.2 adopts the official model with one explicit deferral:

1. **Module side**: a new `modules/savant_security/module.yml` mirrors
   phase3h-zmq's `zeromq_source_bin + nvinfer + nvtracker` shape, but
   replaces the rule pyfunc with R1.1's
   `BehaviorRulesPyFunc` reading C1.1's `cameras.generated.yml` via
   `custom.services.camera_config`.
2. **Adapter side**: C1.2 ships **gstreamer source adapters spawned
   by a CLI controller** (`scripts/runtime/camera_source_controller.py`).
   Container name is fixed at `video-analytics-source-{source_id}` so
   subsequent `stop`/`status` invocations are deterministic.
3. **Retina RTSP Service**: deferred. Its image, command surface, and
   per-stream configuration aren't fully specified in this codebase
   yet; baking them in now would be a guess. The plan doc and
   `sources.generated.yml` schema leave room for an `adapter_type:
   retina` entry without code changes when C1.3/C1.4 picks it up.
4. **Compose**: a new `infra/docker-compose.c1-official-adapter.yml`
   brings up only the long-lived services (redis, postgres, api,
   savant-security, event-worker, sinks, evidence-worker). The
   source-adapter is intentionally NOT a compose service. Controller
   spawns it with `docker run -d --network c1-official-adapter_default`.

The legacy `infra/docker-compose.phase3h-zmq.yml` is untouched. Both
compose projects can coexist on the same host — they use distinct
container-name prefixes (`phase3h-zmq-*` vs `c1-official-*`) and host
ports (`8001`/`5435`/`6382` vs `8004`/`5438`/`6385`).

## 5. Why we do NOT bake RTSP into module.yml

- Tightly coupling the module to one URI defeats Savant's design: the
  module is supposed to be source-agnostic.
- Restarting GPU inference is expensive; restarting an adapter is
  cheap. Every config change should preferably restart the smallest
  process.
- Credentials in RTSP URIs would have to be passed as Savant module
  env. They'd then appear in `docker inspect`, `ps`, and any
  Compose-rendered text. Keeping them in an adapter container limits
  the blast radius.
- The official Retina RTSP Service path is incompatible with module-
  hosted RTSP: Retina runs as its own service that adapters proxy
  through. Hard-wiring RTSP in the module would force a rewrite when
  C1.3 / C1.4 switches to Retina.

## 6. Why Savant does NOT call FastAPI directly

- FastAPI is request-scoped and stateful; Savant's pyfunc is
  GPU-loop-scoped. Synchronous HTTP from inside `process_frame` would
  block the frame queue and induce backpressure.
- One service per blast-radius: a FastAPI outage must not stop event
  generation, and a Savant restart must not interrupt API requests.
- Standard Savant deployments do not assume any business HTTP service
  is reachable. The pyfunc must always be able to start from a config
  on disk.

So the data flow is one-way at config time: `API → file on disk →
loader → pyfunc`. The reverse direction (events) is fully decoupled
through Redis Streams.

## 7. Why C1.2 does NOT do full hot-reload

- The loader (`custom.services.camera_config.load_camera_config`)
  reads the YAML once at pyfunc construction. There is no inotify
  watch, no signal handler, no API callback.
- Hot-reload would require: per-source rule swap atomicity, in-flight
  `TrackStateStore` migration, and graceful cooldown carry-over. None
  of these are blockers for "describe one camera, observe one event",
  but all of them add real failure modes.
- The C1.2 acceptance bar is `config → restart savant-security →
  attach source → evidence`. The savant restart is local (the
  metadata-sink / video-file-sink / API / event-worker stay running),
  so the operator-visible interruption is small.

A future C1.x sub-phase can add SIGHUP-based reload or a config bus
when the requirement materialises.

## 8. Why source lifecycle is the controller's job

- Compose is declarative: "this stack runs these services." Adding a
  camera should NOT mean editing a YAML and re-running `compose up`,
  because that surface area is too wide (it can affect the module,
  postgres, event-worker, ...).
- The controller is the smallest surface that does only `docker run` /
  `docker rm` on adapter containers and validates against the exported
  `sources.generated.yml`.
- Once the controller exists, swapping its backend from `docker run`
  to Retina RTSP API calls is one function change. The CLI surface
  (`start --source-id`, `stop --source-id`, `status --source-id`)
  remains identical.

---

*Written 2026-05-25. Companion to `docs/phase_c1_2_official_adapter_runtime.md`.*
