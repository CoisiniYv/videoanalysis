# R2 File Inventory and Cleanup Plan

Date: 2026-05-29
Status: draft for review; no files should be deleted automatically.

## Scope

R2 pauses feature work and records what should be kept, archived, ignored, or reviewed before the next single-GPU batch performance baseline. This inventory is intentionally conservative: it documents the state of the worktree and gives recommendations, but it does not delete, stage, or commit anything.

Current F4.3 status must be described precisely:

- F4.3 debug end-to-end recognition smoke: PASS.
- F4.3 production evidence pipeline: NOT DONE.

Known limitations:

- Snapshots and video are post-run exports, not realtime event evidence.
- `annotated_hits.mp4` is a debug clip, not a production per-event clip.
- `watchlist_hit` and `live_search_hit` are not implemented.
- There is no FastAPI query or upload entrypoint for the F4.3 recognition flow.
- There is no production media lifecycle for recognition evidence.
- The recognition threshold still needs calibration on more sources.
- YOLO26-pose and YOLOv8-Face appear together in the c1 official module by static review, but runtime log verification should be repeated before performance work.

## F4.3 Script and Document Inventory

| Path | Purpose | Recommendation |
|---|---|---|
| `scripts/smoke/check_f4_3a_local_video_face_observation.sh` | Local mp4 source-adapter smoke that proves video -> Savant -> Redis -> face-worker -> PostgreSQL `face_observations`. | Keep and commit later as a debug smoke. |
| `scripts/smoke/check_f4_3a_visual_evidence.sh` | Wrapper for a single-frame visual overlay export. | Archive/prototype; useful for manual inspection, not production evidence. |
| `scripts/smoke/export_f4_3a_face_observation_snapshots.py` | Reads existing `face_observations`, extracts frames, draws bbox and landmarks. | Archive/prototype or merge into F4.3B exporter if retained. |
| `scripts/smoke/check_f4_3b_registered_person_video_recognition.sh` | Runs local mp4 adapter, collects observations, matches Finch/Reese gallery, creates debug evidence package. | Keep as debug recognition smoke; do not label as production evidence. |
| `scripts/smoke/export_f4_3b_registered_person_evidence.py` | Post-run exporter for snapshots, `annotated_hits.mp4`, `summary.json`, `hits.csv`. | Keep as debug evidence exporter; later split reusable matching/reporting from visualization if production work starts. |
| `scripts/smoke/analyze_f4_3b_similarity_calibration.py` | Calibration report and top-candidate HTML generator. | Keep as calibration/debug tool. |
| `docs/phase_f4_3a_local_video_face_observation_smoke.md` | F4.3A local mp4 observation smoke notes. | Keep and commit later if R2 review accepts F4.3A smoke docs. |
| `docs/phase_f4_3a_visual_evidence_smoke.md` | Manual visual snapshot smoke notes. | Archive/prototype; explicitly not production viewer. |
| `docs/phase_f4_3b_registered_person_video_evidence_smoke.md` | F4.3B debug evidence smoke notes. | Keep, but retain language that it is debug evidence and not production watchlist/live_search. |
| `harness/tests/test_f4_3a_local_video_face_observation_contract.py` | Static contract for F4.3A local video smoke. | Keep and commit later with the smoke script. |
| `harness/tests/test_f4_3a_visual_evidence_contract.py` | Static contract for visual snapshot debug tool. | Archive/prototype or keep if the visual script remains. |
| `harness/tests/test_f4_3b_registered_person_video_evidence_contract.py` | Static contract for F4.3B debug evidence package. | Keep and commit later; ensure naming says debug smoke. |

## Unrelated Dirty or Pre-Existing Files

| Path | Observed state | Recommendation |
|---|---|---|
| `.gitignore` | Modified before R2. | Review separately; likely keep and commit later only if it ignores generated outputs such as `tmp/`, `manual-inspection/`, and media artifacts. |
| `services/face-worker/app/match_repository.py` | Modified before R2. | Unrelated pre-existing change; do not revert in R2. Review in F4.1/F4 gallery matching context. |
| `specs/05_database_schema.md` | Modified before R2. | Unrelated pre-existing schema documentation change; review separately. |
| `db/migrations/008_phase_f4_1_match_results_partial_gallery_unique.sql` | Untracked. | Keep and commit later only with the F4.1 match-results migration set. |
| `docs/phase_f4_1_find_person_with_clip_mvp.md` | Untracked. | F4.1 prototype doc; archive/prototype until F4.1 is mainlined. |
| `harness/tests/test_f4_1_find_person_contract.py` | Untracked. | F4.1 prototype contract; keep with F4.1 branch or archive. |
| `scripts/demo/` | Untracked directory. | Archive/prototype; do not include in production mainline without review. |
| `services/face-worker/app/find_person_repository.py` | Untracked. | F4.1 prototype implementation; review separately. |
| `services/face-worker/find_person.py` | Untracked. | F4.1 prototype CLI; review separately. |
| `face/` | Untracked test image directory. | Leave untracked; add to `.gitignore` if not already ignored; never commit test photos. |

## Suggested Cleanup Actions for Review

1. Keep F4.3A local video smoke and F4.3B debug recognition smoke as smoke tools, with docs that clearly mark them as debug evidence.
2. Merge or archive the F4.3A one-frame visual tool after F4.3B exporter is accepted, because F4.3B already generates richer snapshots.
3. Keep calibration analysis as a developer-only tool for threshold and gallery-quality review.
4. Leave `face/`, `testVideo/`, `manual-inspection/`, `/data/video-analytics/media`, and generated `tmp/` output out of git.
5. Do not delete any file automatically. Deletion should happen only after manual review and a separate cleanup approval.

