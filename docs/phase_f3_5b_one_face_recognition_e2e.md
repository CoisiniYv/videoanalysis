# Phase F3.5b — One Face Recognition End-to-End Smoke

## Purpose

Prove that one registered-person recognition loop works end-to-end using
real data from the running system.

```
face_observation (existing, from real video pipeline)
  -> enroll_gallery.py (register as test person)
  -> match_gallery.py (search gallery)
  -> match_results (write)
  -> top1 = test person (verify)
  -> cleanup
```

## What This Proves

- `enroll_gallery.py` can register a real face_observation as a gallery embedding
- `match_gallery.py` can match an observation against the gallery
- `match_results` receives correct `gallery_match` rows with proper semantics
- The same face enrolled and queried produces top1 self-match (similarity >= 0.99)
- Cleanup removes all test data without touching original observations

## What This Does NOT Prove

- This is **not** a trajectory query test — see F3.6 for `registered_person_history`
- This does **not** test F3.6 trajectory query harness
- This only verifies gallery enrollment + gallery match + match_results

## Deterministic Test ID

The smoke uses a fixed search_request_id to ensure reproducible cleanup:

```
f35b0000-0000-4000-8000-000000000001
```

This UUID is never randomly generated. All cleanup and verification queries
reference this constant.

## Script

`scripts/smoke/check_f3_5b_one_face_recognition_e2e.sh`

Steps:
1. Select one existing face_observation (most recent with 512-dim embedding)
   - Save both observation UUID (`id`) and `source_observation_id`
2. Enroll as `test:f3_5b:person` via `enroll_gallery.py --external-person-id`
3. Match via `match_gallery.py --observation-id --top-k 5 --min-similarity 0.5`
   - Uses deterministic `--search-request-id f35b0000-0000-4000-8000-000000000001`
4. Verify match_results:
   - rank=1 exists
   - top1 person = test person
   - top1 gallery_embedding = newly created
   - `query_observation_id` == selected observation UUID (equality, not just non-empty)
   - `query_source_observation_id` == selected source_observation_id (equality)
   - similarity >= 0.99 (enforced — fail if below)
   - `matched_observation_id = NULL` (gallery_match semantics, per F3.5)
5. Print camera context (camera_id, source_id, track_id, timestamp_ms)
6. Print full recognition summary
7. Trap-based cleanup: remove test person, gallery, match_results
   - Cleanup uses deterministic search_request_id, not temp file
8. Verify original face_observation not deleted

## Limitations

- Uses the **same** observation for enrollment and query — this proves
  plumbing works but does not test cross-frame or cross-camera robustness
- Does not test local image upload enrollment (future work)
- Does not test watchlist_hit or live_search_hit (future work)
- Does not export evidence snapshots or video (future work)

## What This Phase Does NOT Do

- Does NOT implement local image upload
- Does NOT implement watchlist_hit or live_search_hit
- Does NOT write to security.events
- Does NOT provide a REST API
- Does NOT implement retention cleanup
- Does NOT change Savant pipeline or Redis producer
- Does NOT test trajectory query (that is F3.6)
