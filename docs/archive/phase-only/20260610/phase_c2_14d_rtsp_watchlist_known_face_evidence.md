# C2.14D RTSP Watchlist Known-Face Evidence

## Scope

C2.14D closes the gap left by C2.13R and C2.14C: it proves a real RTSP
`watchlist_hit` / known-face event can be converted into a video evidence bundle
with an identity-bound sidecar.

The accepted identities remain the externally registered C2 identities:

- Reese: `person_id=5`, `external_person_id=demo:f4_3:reese`,
  `gallery_embedding_id=4`
- Finch: `person_id=6`, `external_person_id=demo:f4_3:finch`,
  `gallery_embedding_id=5`

## Evidence Path

C2.14D uses the segment ring evidence path:

```text
Redis security.face_observations
  -> PostgreSQL gallery read
  -> threshold-passing Reese/Finch match
  -> watchlist_hit event object
  -> RTSP segment ring clip
  -> sink_metadata.json
  -> annotations.frame_cache.identity.jsonl
  -> summary/report
```

It does not use event-style Replay and does not use broad DB window fallback for
visual or identity binding.

## Identity Binding

The builder first checks existing PostgreSQL `watchlist_hit` rows. If no
threshold-passing hit is covered by the current ring, it scans bounded Redis
`security.face_observations`, reads only Reese/Finch gallery embeddings from
PostgreSQL, recomputes cosine similarity, and accepts only
`similarity >= threshold`.

The sidecar identity patch is written only when the selected observation can be
bound to ring metadata by direct frame identity:

- `direct_frame_uuid`, or
- `direct_frame_pts`.

The sidecar patch contains only identity metadata:

- `source_observation_id`
- `event_type=watchlist_hit`
- `person_id`
- `external_person_id`
- `gallery_embedding_id`
- `similarity` / `match_score`
- `threshold`
- `matched=true`
- `identity_binding_method`

It does not write embedding vectors, image bytes, base64 images, or face crop
bytes to sidecar, summary, or reports.

## Runtime Caveats

- Runtime module provenance is not normalized yet; live runtime may still depend
  on `/tmp/c2-fps-probe-module`.
- Native post-Savant sink metadata may contain Savant feature vectors. C2.14D
  uses them only as input metadata and does not copy embeddings into the sidecar
  or reports.
- `/data/video-analytics` is not cleaned.
- Worker/API product integration is not restored in this phase.

## Validation

Required commands:

```bash
python -m pytest harness/tests/test_c2_14d_rtsp_watchlist_known_face_evidence.py -q
python -m py_compile scripts/tools/build_c2_14d_watchlist_clip_from_ring.py
python -m py_compile scripts/tools/audit_c2_14d_watchlist_evidence_bundle.py
bash -n scripts/smoke/current/check_c2_14d_rtsp_watchlist_known_face_evidence.sh
git diff --check
scripts/smoke/current/check_c2_14d_rtsp_watchlist_known_face_evidence.sh
```

Expected pass marker:

```text
PASS_C2_14D_RTSP_WATCHLIST_KNOWN_FACE_EVIDENCE_READY
```
