# Midterm Deployment and Operator Guide

This guide covers startup, camera configuration, runtime presets, status checks, and shutdown for deployment staff and daily operators of the Video Analytics Platform. Start with the root [`README.md`](README.md) for the project overview and architecture.

## Start the System

```bash
bash scripts/midterm_start.sh
```

The startup script checks the runtime environment, prepares data directories, starts base images/services, and prepares runtime components managed by the operator console on port 8090.

After startup, open:

```text
http://127.0.0.1:8090/operator
```

Port 8090 is the unified browser entry point. Internal API, Redis, inference, and metrics ports are primarily used for service communication or diagnostics.

## Recommended Workflow

### 1. Register Cameras

Under **Configuration → Cameras**, configure each stream with:

- Camera name and RTSP address;
- ROI；
- Algorithms to enable;
- Event rules and related parameters.

For a batch startup of the full dual-branch pipeline, configure cameras first, then select all cameras to run.

### 2. Select a Runtime Preset

Select cameras and a hardware preset on the **Startup and Runtime** page:

| Preset | Reference Streams | Analysis Frame Rate | Branch Allocation |
| --- | ---: | ---: | --- |
| `production_t4_40` | 40 | 4 FPS | A/B 20/20 |
| `local_4090_60` | 60 | 8 FPS | A/B 30/30 |

The page supports automatic balancing and manual A/B source assignment. Actual capacity depends on resolution, bitrate, encoding settings, event density, GPU model, and storage performance.

### 3. Start the Full Pipeline

Starting the full pipeline prepares the runtime, brings sources into the expected state, establishes the rolling cache, and enables the evidence pipeline. Use actual sources, FPS, queues, latency, and task status reported by the 8090 page to assess runtime health.

### 4. View Analysis Results

The operator console provides:

- **Cameras**: RTSP, ROI, algorithm, and rule management;
- **People and Faces**: person profiles, face registration, and gallery management;
- **Evidence**: event videos, snapshots, bounding boxes/poses, and timelines;
- **Person Trajectories**: person match records and trajectory images;
- **Startup and Runtime**: presets, sources, throughput, and latency;
- **Advanced Maintenance**: storage, topology, and runtime diagnostics.

## Runtime Architecture

```text
RTSP Cameras
    |
    v
Replay A/B -> Raw Fan-out A/B
    |                 |
    |                 +--> full-rate stream -> Rolling Cache
    |
    +--> sampled stream -> Savant / GPU inference
                              |
                              +--> events / person observations
                              +--> face ROI -> AdaFace -> Face Worker

Events + Rolling Cache
          |
          v
     Media Worker
          |
          v
 Evidence + PostgreSQL index -> 8090 Viewer
```

Full presets retain full-frame-rate encoded streams in the rolling cache while AI analysis runs at the configured frame rate, decoupling inference throughput from evidence video quality.

## Runtime Checks

Common commands:

```bash
bash scripts/midterm_health.sh
bash scripts/runtime/doctor_midterm.sh
```

Also confirm on the 8090 console that:

- All target cameras are assigned to the expected branches;
- Every source continues to produce new frames;
- Analysis FPS matches the preset without a sustained decline;
- Queue / retry / failure metrics are not continuously increasing;
- Evidence tasks progress from queued to completed or another explicit terminal state;
- Event evidence, snapshots, and person trajectories can be queried successfully.

Use `scripts/runtime/report_evidence_camera_ledger.py` to inspect the evidence lifecycle by camera.

## Stop the System

To stop only the current acquisition/analysis runtime, use **Stop Full Pipeline** on port 8090. This stops the corresponding acquisition and inference components while allowing necessary background tasks to finish.

To stop all services:

```bash
bash scripts/midterm_stop.sh
```

## Main Configuration Files

| File | Purpose |
| --- | --- |
| `infra/docker-compose.midterm.yml` | Main service orchestration |
| `infra/env/midterm.env` | Default environment variables |
| `infra/midterm-storage.override.yml` | Data and media storage mounts |
| `infra/operator-dual-runtime.override.yml` | A/B dual-branch runtime |
| `modules/savant_replay/config.midterm*.json` | Replay configuration |
| `modules/savant_security/config/cameras.midterm.yml` | Camera runtime snapshot |
| `modules/savant_security/module.yml` | Savant inference module |

Exact preset parameters are maintained by `RUNTIME_PROFILE_PRESETS` in `services/api/app/services/runtime_topology.py`.

## Further Reading

- [`docs/README.md`](docs/README.md): documentation entry point
- [`docs/current_architecture.md`](docs/current_architecture.md): complete system architecture
- [`docs/midterm_deployment.md`](docs/midterm_deployment.md): deployment and storage
- [`docs/midterm_web_operator_guide.md`](docs/midterm_web_operator_guide.md): operating the 8090 console
- [`docs/midterm_quick_reference.md`](docs/midterm_quick_reference.md): common commands and troubleshooting
- [`docs/frontend_interface/README.md`](docs/frontend_interface/README.md): frontend and API integration

Internal FastAPI documentation is available at the API service's own `/docs` endpoint. Port 8090 primarily serves the operator interface and business API/media proxy. Do not use `http://127.0.0.1:8090/docs` as the API documentation entry point.
