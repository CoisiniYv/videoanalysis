# P1a Replay Inline Pass-through Topology

P1a is a POC-only verification of the single inline path:

```text
testVideo/test.mp4
  -> source-adapter
  -> replay-service
  -> savant-security
  -> Redis security.events
```

It does not generate clips and does not start `video-file-sink`,
`clip-worker`, `media-worker`, `event-worker`, PostgreSQL, an RTSP server, or
source extraction fallback tooling.

## Streams

| Hop | Stream |
|---|---|
| Replay input `in_stream` | `router+bind:tcp://0.0.0.0:5555` |
| Source adapter output | `dealer+connect:tcp://replay-service:5555` |
| Replay output `out_stream` | `dealer+connect:tcp://savant-security:5557` |
| Savant input | `router+bind:tcp://0.0.0.0:5557` |

The POC uses `modules/savant_replay/config.p1a_inline.json` because the
existing shared Replay config keeps `out_stream` set to `null` for legacy
Replay cache/clip POCs. P1a needs a non-null `out_stream` to prove that Replay
can forward the same Savant protocol stream downstream to the module.

## Runtime

Run:

```bash
bash scripts/smoke/check_p1a_replay_inline_pass_through.sh
```

The smoke starts only:

- `p1a-replay-inline-redis`
- `p1a-replay-inline-replay-service`
- `p1a-replay-inline-savant-security`
- `p1a-replay-inline-source-adapter`

It verifies:

- Replay is running and exposes `/api/v1/status`.
- The source adapter sends only to Replay.
- Replay has a non-null `out_stream`.
- Savant is configured to receive from the Replay `out_stream`.
- Redis `security.events` receives output for `source_id=p1a_replay_inline`.
- No second RTSP pull, video-file-sink, clip-worker, media-worker, clip
  generation, or source extraction fallback is used.

## Latest Local Verification

Verified on 2026-05-31 with:

```bash
python3 -m pytest harness/tests/test_p1a_replay_topology_contract.py -q
bash scripts/smoke/check_p1a_replay_inline_pass_through.sh
```

Result: `PASS`

Observed topology:

- `source-adapter -> replay-service`: yes
- `replay-service -> savant-security`: yes
- Replay `out_stream`: `dealer+connect:tcp://savant-security:5557`
- Savant input stream: `router+bind:tcp://0.0.0.0:5557`
- Redis `security.events`: observed for `source_id=p1a_replay_inline`
- Second RTSP pull used: no

## Boundary

P1a is not a production compose and does not modify the production C1 official
adapter compose. It is a topology proof only. If the smoke cannot observe
events through `source-adapter -> replay-service -> savant-security`, it must
report `BLOCKED` instead of substituting a dual-source or extraction path.

No production compose is changed by this POC.
