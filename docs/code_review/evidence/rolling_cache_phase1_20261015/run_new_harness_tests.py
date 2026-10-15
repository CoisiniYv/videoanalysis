"""Run the exact JUnit testcase difference with the frozen harness interpreter."""

import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET


def identities(filename):
    return {
        (case.get("classname", ""), case.get("name", ""))
        for case in ET.parse(filename).getroot().iter("testcase")
    }


baseline = identities("/tmp/pressure60-baseline-stable.xml")
current = identities("/tmp/pressure60-final.xml")
added = sorted(current - baseline)
nodeids = [classname.replace(".", "/") + ".py::" + name for classname, name in added]
Path("/tmp/pressure60-final-new-nodeids.json").write_text(
    json.dumps(nodeids, indent=2) + "\n", encoding="utf-8"
)
env = dict(os.environ)
env.pop("DATABASE_URL", None)
env.pop("VIDEO_ANALYTICS_DATABASE_URL", None)
command = [
    "/tmp/pressure60-harness-env/bin/python", "-m", "pytest", "-q",
    *nodeids, "--junitxml=/tmp/pressure60-final-new-tests.xml",
]
with open("/tmp/pressure60-final-new-tests.log", "w", encoding="utf-8") as output:
    result = subprocess.run(command, env=env, stdout=output, stderr=subprocess.STDOUT)
print(json.dumps({"selected": len(nodeids), "returncode": result.returncode}))
raise SystemExit(result.returncode)
