# Frontend and API Integration

This directory describes the Video Analytics Platform's Web operator console, browser-side API usage, and main data contracts. It is intended for developers modifying the 8090 interface, integrating business APIs, or understanding frontend/backend boundaries.

Read [`../current_architecture.md`](../current_architecture.md) first for the overall architecture.

## Frontend Architecture

```text
Browser :8090
  |
  v
Evidence Viewer / Operator
  +-- /static/*       HTML / CSS / JavaScript
  +-- /api/v1/*       -> FastAPI service
  +-- /media/*        -> evidence media proxy
  +-- /api/bundles/*  legacy compatibility path
```

The browser uses same-origin API and media paths through port 8090. FastAPI runs on the internal Compose network; the browser does not need direct access to its container port.

## Frontend Stack

The current console uses a lightweight native frontend:

- HTML;
- CSS;
- Vanilla JavaScript;
- No React / Vue / TypeScript;
- No separate frontend bundler.

Main scripts load in this order:

```text
operator.js -> trajectory.js -> evidence.js -> maintenance.js
```

`index.html` defines the DOM structure, and `style.css` provides global styles. When changing static assets, check page references and asset version parameters together.

## Documentation Navigation

| Document | Contents |
| --- | --- |
| [01_architecture.md](01_architecture.md) | 8090 proxy, page modules, and runtime control architecture |
| [02_api_inventory.md](02_api_inventory.md) | Browser-side API inventory |
| [03_data_contracts.md](03_data_contracts.md) | Camera, person, trajectory, and evidence data contracts |
| [04_views_and_modules.md](04_views_and_modules.md) | Views, DOM, and module interactions |
| [05_development_and_validation.md](05_development_and_validation.md) | Development and validation |

## Page Modules

The console includes these areas:

- **Configuration**: cameras, ROIs, algorithms, and rules;
- **People and Faces**: person profiles and face galleries;
- **Evidence**: event evidence, videos, snapshots, and annotations;
- **Person Trajectories**: observations over time by person;
- **Startup and Runtime**: presets, camera selection, sources/FPS/latency, and task status;
- **Advanced Maintenance**: topology, performance configuration, storage, and diagnostics.

Hash-based navigation switches between modules:

```text
#cameras
#people
#trajectory
#evidence
#runtime
#maintenance
```

## API Integration Conventions

1. Prefer `/api/v1/*` for browser business requests.
2. Use the `/media/...` paths returned by the API for evidence media.
3. Use `/api/v1/evidence` for new evidence features; `/api/bundles` exists only for legacy data compatibility.
4. Obtain runtime presets and parameters from the server API. The frontend should not maintain a second hardcoded preset list.
5. Submit selected cameras as `source_ids` when starting the full pipeline in a batch.
6. FastAPI's OpenAPI `/docs` endpoint belongs to the internal API service and is not a primary user entry point on port 8090.
7. Browser use of `/api/v1/ws/alerts` requires WebSocket upgrade support in the deployment proxy.

## Code Locations

Frontend static pages and the proxy service live under `services/evidence-viewer/`. Business APIs live under `services/api/`. Changes to interface fields, runtime control, or evidence structures should also check:

```text
services/evidence-viewer/
services/api/
services/event-worker/
services/media-worker/
harness/tests/
```

Code and automated tests jointly constrain API contracts and page behavior. This document explains stable integration practices rather than temporary branch or workspace states.

## Deployment Security

Port 8090 provides a unified operator entry point. On untrusted networks, place it behind a controlled network or reverse proxy and configure TLS, access controls, and appropriate auditing. Do not expose internal Redis, PostgreSQL, inference, or debugging ports directly to the public Internet.
