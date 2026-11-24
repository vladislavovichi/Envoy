import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
failed = []
for package in ("journal", "crypto", "engine"):
    summaries = [
        data["summary"]
        for path, data in report["files"].items()
        if f"/envoy/{package}/" in path.replace("\\", "/")
    ]
    total = sum(summary["num_branches"] for summary in summaries)
    covered = sum(summary["covered_branches"] for summary in summaries)
    percentage = 100 * covered / total if total else 100
    if not summaries or percentage < 90:
        failed.append(package)
    print(f"{package}: {percentage:.2f}% branches ({covered}/{total})")
sys.exit(1 if failed else 0)
