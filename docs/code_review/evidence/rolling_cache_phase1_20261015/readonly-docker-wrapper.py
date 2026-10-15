#!/usr/bin/env python3
"""Forward only read-only Docker calls, rendering local Compose bytes via memfd."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import yaml

args = sys.argv[1:]
if args and args[0] == "inspect":
    raise SystemExit(subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "uos157", "docker", *args]).returncode)
if not args or args[0] != "compose" or "config" not in args:
    raise SystemExit("read-only validation wrapper permits only docker inspect / compose config")

files = []
env_file = None
index = 1
while index < len(args):
    arg = args[index]
    if arg in ("-f", "--file", "--env-file"):
        path = Path(args[index + 1]).resolve()
        if arg == "--env-file":
            env_file = str(path)
        else:
            files.append(path)
        index += 2
    elif arg == "config":
        trailing = args[index + 1:]
        break
    else:
        raise SystemExit(f"unsupported Compose option {arg}")
else:
    raise SystemExit("missing config operation")

if not files:
    raise SystemExit("missing Compose files")
all_env = {}
if env_file:
    all_env[env_file] = Path(env_file).read_text(encoding="utf-8")
documents = []
for path in files:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for service in (doc.get("services") or {}).values():
        if not service.get("env_file"):
            continue
        entries = service["env_file"]
        if isinstance(entries, str):
            entries = [entries]
        normalized = []
        for entry in entries:
            if not isinstance(entry, str):
                raise SystemExit("env_file mapping unsupported by validation wrapper")
            resolved = str((path.parent / entry).resolve())
            all_env[resolved] = Path(resolved).read_text(encoding="utf-8")
            normalized.append(resolved)
        service["env_file"] = normalized
    documents.append(doc)

remote = r'''
import ctypes, json, os, subprocess, sys, yaml
payload = json.load(sys.stdin)
fds = []
def memfile(name, value):
    if hasattr(os, 'memfd_create'):
        fd = os.memfd_create(name, 0)
    else:
        fd = ctypes.CDLL(None, use_errno=True).memfd_create(name.encode(), 0)
        if fd < 0:
            raise OSError(ctypes.get_errno(), 'memfd_create failed')
    os.write(fd, value.encode())
    os.lseek(fd, 0, 0)
    fds.append(fd)
    return '/proc/self/fd/' + str(fd)
env_paths = {path: memfile('pressure60-env', text) for path, text in payload['env'].items()}
cmd = ['docker', 'compose', '--project-directory', payload['project_directory']]
if payload['env_file']:
    cmd += ['--env-file', env_paths[payload['env_file']]]
for doc in payload['documents']:
    for service in (doc.get('services') or {}).values():
        if service.get('env_file'):
            service['env_file'] = [env_paths[path] for path in service['env_file']]
    cmd += ['-f', memfile('pressure60-compose', yaml.safe_dump(doc, sort_keys=False))]
cmd += ['config'] + payload['trailing']
result = subprocess.run(cmd, pass_fds=fds)
raise SystemExit(result.returncode)
'''
payload = {"env": all_env, "env_file": env_file, "documents": documents,
           "project_directory": str(files[0].parent), "trailing": trailing}
command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "uos157", "python3 -c " + shlex.quote(remote)]
raise SystemExit(subprocess.run(command, input=json.dumps(payload), text=True).returncode)
