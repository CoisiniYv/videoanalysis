# Infra Archive

Archived files here are historical phase-only runtime entrypoints.

- `phase-only/20260602/` contains compose files moved out of root `infra/` during C1H.0.
- `phase-only/20260609/` contains former C1/C2 root compose files moved out of the deploy surface when the midterm project version was promoted.
- Existing `docker-compose.phase*.yml` files under this directory are also historical phase-only compose files.
- Do not use archived compose files for current runtime work or acceptance.

Current deployment compose:

```text
infra/docker-compose.midterm.yml
```
