# Video Analytics Technical Reference

This directory collects documentation on the Video Analytics Platform's system structure, runtime data flows, control plane, evidence pipeline, data contracts, performance tuning, and troubleshooting. It is intended for developers, deployment staff, and maintainers who need a detailed understanding of service responsibilities and runtime behavior.

If you are new to the project, start with the root [`README.md`](../../README.md) and [`../README.md`](../README.md).

## Recommended Reading Order

| Document | Description |
| --- | --- |
| [00_Index.md](00_Index.md) | Technical reference index |
| [01_System_Overview.md](01_System_Overview.md) | System capabilities and components |
| [02_Runtime_Data_Flow.md](02_Runtime_Data_Flow.md) | Video, event, face, and evidence data flows |
| [03_Module_Map.md](03_Module_Map.md) | Module and service responsibilities |
| [04_Control_Plane_8090.md](04_Control_Plane_8090.md) | 8090 control plane |
| [05_Evidence_Chain.md](05_Evidence_Chain.md) | Evidence lifecycle and media generation |
| [10_Glossary.md](10_Glossary.md) | Glossary |
| [11_Data_Contracts_And_Storage.md](11_Data_Contracts_And_Storage.md) | PostgreSQL, Redis, and media storage contracts |
| [12_Service_Deep_Dive.md](12_Service_Deep_Dive.md) | Core services in detail |
| [13_Runtime_Control_Runbook.md](13_Runtime_Control_Runbook.md) | Runtime control and operating procedures |
| [14_Performance_And_Acceptance_Playbook.md](14_Performance_And_Acceptance_Playbook.md) | Performance testing and acceptance methods |
| [15_Design_Invariants_And_Decisions.md](15_Design_Invariants_And_Decisions.md) | Key design constraints |
| [16_Troubleshooting_Playbook.md](16_Troubleshooting_Playbook.md) | Troubleshooting methods |
| [17_Testing_And_Change_Guide.md](17_Testing_And_Change_Guide.md) | Testing and change checks |
| [18_Artifact_And_Directory_Map.md](18_Artifact_And_Directory_Map.md) | Runtime artifacts and directories |

## Sources of Truth

To distinguish documentation from runtime state, the project uses these conventions:

- **PostgreSQL**: persistent source of truth for cameras, rules, people, face galleries, events, and evidence metadata;
- **Redis**: asynchronous messages, streams, and runtime task transport;
- **Runtime YAML / JSON**: generated from persistent configuration or used to represent the current runtime snapshot;
- **Source code and database migrations**: definitive technical definitions of interfaces, lifecycles, and schemas;
- **Automated tests**: executable verification of key contracts and regression behavior.

The full dual-branch runtime uses the rolling cache as its main evidence media source. Replay job paths remain available for compatibility or specific runtime modes. Face vectors use pgvector by default; Qdrant is available through the corresponding deployment profile.

## Engineering History

Some documents record performance optimization, migrations, and historical design context, including:

- `06_Performance_Optimization_History.md`;
- `07_Deployment_Migration.md`;
- `08_Open_Risks_And_Next_Actions.md`.

These explain system evolution and do not replace current architecture, deployment configuration, interface code, or runtime presets. Dated load-test results apply only to the measured hardware, inputs, and code version.

The repository may also retain historical onboarding or collaboration materials for specific development workflows. These are not required for operation or deployment and are not primary entry points for this reference.

## Documentation Maintenance

- Update stable documentation when architecture, data contracts, or operating procedures change;
- Include the test environment, input scale, and runtime parameters with performance data;
- Preserve context for historical diagnostic conclusions while treating current code and configuration as authoritative;
- Keep external documentation independent of temporary branches, individual workspace states, and uncommitted changes;
- Update data contracts and related tests when changing schemas, Redis streams, worker inputs/outputs, or evidence lifecycles.
