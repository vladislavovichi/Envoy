import json
import subprocess
import sys

import pytest


@pytest.mark.parametrize(("covered", "expected"), [(9, 0), (8, 1)])
def test_coverage_gate_checks_branch_percentage_per_package(tmp_path, covered, expected) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "files": {
                    "src/envoy/journal/chain.py": {
                        "summary": {"num_branches": 10, "covered_branches": covered}
                    },
                    "src/envoy/crypto/keys.py": {
                        "summary": {"num_branches": 0, "covered_branches": 0}
                    },
                    "src/envoy/engine/engine.py": {
                        "summary": {"num_branches": 4, "covered_branches": 4}
                    },
                }
            }
        )
    )
    result = subprocess.run(
        [sys.executable, "scripts/check_coverage.py", str(report)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
