# Documentation

This is the documentation entry point for the Video Analytics Platform. The repository contains stable usage guides alongside design, load-test, diagnostic, and migration records. To deploy, use, or understand the current system, start with the stable documents listed here.

## User Entry Points

| Document | Audience | Contents |
| --- | --- | --- |
| [`../README.md`](../README.md) | Everyone | Project overview, architecture, and quick start |
| [`../README_MIDTERM.md`](../README_MIDTERM.md) | Deployment staff / operators | Startup, camera configuration, runtime presets, and shutdown |
| [`midterm_web_operator_guide.md`](midterm_web_operator_guide.md) | Operators | Detailed guide to the 8090 Web console |
| [`midterm_quick_reference.md`](midterm_quick_reference.md) | Operations staff | Common commands and troubleshooting |
| [`midterm_deployment.md`](midterm_deployment.md) | Deployment staff | Compose, storage, profiles, and environment configuration |

## Architecture and Development

| Document | Contents |
| --- | --- |
| [`current_architecture.md`](current_architecture.md) | Current components, data flows, and runtime modes |
| [`frontend_interface/README.md`](frontend_interface/README.md) | Web frontend, 8090 proxy, and API integration |
| [`midterm_knowledge_base/README.md`](midterm_knowledge_base/README.md) | Technical reference for services, evidence pipelines, data contracts, and troubleshooting |
| [`compose_inventory.md`](compose_inventory.md) | Compose files and runtime mode index |

## Documentation Categories

### Stable Documentation

Stable documents describe current usage, architecture, and interfaces, and should be maintained as code and configuration change. This includes the root README, this page, deployment guides, operator guides, and interface documentation.

### Engineering Records

The repository also retains:

- Dated load-test, validation, migration, and diagnostic reports;
- Focused review records under `code_review/`;
- Historical repair materials under `repair_goal/`, `runtime_stability_fix/`, `replay_evidence_fix/`, and similar directories;
- Historical configuration and phase artifacts under `archive/` and nested `archive/phase-only/` directories.

These records preserve the context of engineering decisions and help investigate past issues or implementations. They **do not define current deployment instructions, API contracts, or capacity guarantees**.

## Determining Current Behavior

When documents disagree, check the following in order:

1. Current source code and database migrations on `main`;
2. Current Compose configuration, environment variables, and runtime profiles;
3. Automated contract and regression tests;
4. `current_architecture.md` and stable deployment/operator documentation;
5. Dated historical reports and design records.

When interpreting performance figures, verify the GPU, video resolution, bitrate, analysis FPS, camera count, event load, and code version. Historical load-test figures are not performance guarantees for other environments.

## Common Runtime Commands

```bash
# Start
bash scripts/midterm_start.sh

# Health / diagnostics
bash scripts/midterm_health.sh
bash scripts/runtime/doctor_midterm.sh

# Stop
bash scripts/midterm_stop.sh
```

Web operator console:

```text
http://127.0.0.1:8090/operator
```

For production or untrusted-network deployments, protect port 8090 through a controlled network or reverse proxy. Avoid directly exposing internal database, Redis, inference, or debugging ports.
