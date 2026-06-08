# Runtime Rebaseline: Canonical Repo Restore

Date: 2026-06-08

## Why this restore was done

The canonical project path, `/home/user/video-analytics`, had become a heavily
dirty `master` worktree with hundreds of mixed runtime, test, documentation,
and generated changes. Active C2 runtime work had moved to the clean worktree at
`/tmp/video-analytics-c2-fps-probe`, which made the canonical path unsafe for
continued development.

This restore moves the dirty canonical worktree aside intact and rebuilds
`/home/user/video-analytics` from the clean C2 baseline.

## Dirty main archive

The previous dirty canonical worktree was moved, not deleted:

```text
/home/user/video-analytics_dirty_archive_20260608T045921Z
```

All tracked and untracked files from the dirty `master` worktree remain in that
archive for later manual inspection.

## New canonical baseline

The new canonical repo is based on:

```text
branch: c2/post-savant-poc
commit: 580bf7749a2ad756ab95d4531dfbc8b0805de25e
subject: feat(c2): write rtsp ring segments and build event clips
```

## Why dirty main was not automatically merged

The dirty `master` worktree contained unrelated and interleaved changes across
runtime services, Savant module code, migrations, docs, tests, scripts, and
generated artifacts. Automatically merging or cherry-picking that state would
have mixed historical C1/C1J/C1L work into the C2 baseline and made the new
canonical runtime path ambiguous.

The old worktree was therefore archived for manual recovery only.

## Manual migration candidates from the archive

Review these later before deciding whether to port them:

- `record_request` persistence
- C1J frame annotation docs
- C1L DB reconnect
- `recording_gate`
- `replay_window_planner`
- operator frontend changes
- media-worker dirty fixes
- face-worker dirty fixes

## Runtime data

`/data/video-analytics` was not cleaned or modified by this restore. Existing
media, replay, postgres, redis, model, log, and evidence data remain in place.

## Runtime caveat

The live C2 runtime may still mount the generated Savant module copy:

```text
/tmp/c2-fps-probe-module
```

Follow-up R0.4 should make runtime module generation deterministic or switch the
runtime to mount the repo module directly.

## Next recommendations

- Keep the archived dirty main frozen until manual triage.
- Continue C2 work from `/home/user/video-analytics`.
- Do not automatically merge the archived dirty main into the C2 baseline.
- Use a later runtime code map document to record the final canonical runtime
  mount strategy after R0.4.
