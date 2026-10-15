# Authorized Repair Predecessor

This directory preserves the exact evidence and document saved before the user
authorized repairs to the six additional harness failures. Its current suite
had 17 failures, six absent from baseline 98d388c. These historical files are
not the acceptance results for the repaired branch.

The original `MANIFEST.sha256` is a copy of the parent directory's manifest at
that time, including references to baseline and minimum-check artifacts that
remain in the parent directory. `HISTORY.sha256` hashes this historical copy.

The subsequent authorized repair is recorded as round 1 in the current
`comparison.json`; current JUnit results and exact failure identities remain
in the parent directory.
