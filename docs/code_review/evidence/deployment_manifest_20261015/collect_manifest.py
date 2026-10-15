"""Read-only deployment fingerprint; run over SSH stdin, never write remotely."""
import datetime
import hashlib
import json
import os
import stat

ROOT = "/home/user/video-analytics"
EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    "logs", "log", "data", "media", "artifacts", "datasets", "recordings",
    "testVideo", ".sync-backup-20260714",
}
EXCLUDED_SUFFIXES = (
    ".log", ".log.gz", ".pyc", ".zip", ".tar", ".tar.gz", ".tgz",
    ".mp4", ".mkv", ".ts", ".onnx", ".engine", ".plan", ".pt", ".pth",
    ".safetensors", ".db", ".sqlite", ".sqlite3",
)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


result = {"root": ROOT, "started_at_utc": now(), "files": [], "symlinks": [],
          "excluded": [], "errors": [], "unstable": [],
          "policy": {"directories_any_depth": sorted(EXCLUDED_DIRS),
                     "suffixes": EXCLUDED_SUFFIXES, "follow_symlinks": False}}
for parent, dirs, files in os.walk(ROOT, followlinks=False,
                                   onerror=lambda exc: result["errors"].append(str(exc))):
    for name in sorted(dirs + files):
        path = os.path.join(parent, name)
        relative = os.path.relpath(path, ROOT)
        try:
            before = os.lstat(path)
            if stat.S_ISLNK(before.st_mode):
                result["symlinks"].append({"path": relative, "target": os.readlink(path)})
                if name in dirs:
                    dirs.remove(name)
                continue
            if name in dirs:
                if name in EXCLUDED_DIRS:
                    result["excluded"].append({"path": relative, "reason": "directory"})
                    dirs.remove(name)
                continue
            if name.lower().endswith(EXCLUDED_SUFFIXES):
                result["excluded"].append({"path": relative, "reason": "suffix"})
                continue
            if not stat.S_ISREG(before.st_mode):
                result["excluded"].append({"path": relative, "reason": "not_regular"})
                continue
            digest = hashlib.sha256()
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = os.lstat(path)
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size, after.st_mtime_ns, after.st_ino):
                result["unstable"].append(relative)
            result["files"].append({"path": relative, "sha256": digest.hexdigest(),
                                    "size": before.st_size,
                                    "mtime_ns": before.st_mtime_ns})
        except OSError as exc:
            result["errors"].append({"path": relative, "error": str(exc)})
result["files"].sort(key=lambda entry: entry["path"])
result["finished_at_utc"] = now()
result["file_count"] = len(result["files"])
print(json.dumps(result, ensure_ascii=True, indent=2))
