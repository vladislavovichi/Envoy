import subprocess
import sys


def run_contracts(config: str = ".importlinter") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports; import sys; "
            "sys.exit(lint_imports(config_filename=sys.argv[1]))",
            config,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_import_contracts_hold() -> None:
    result = run_contracts()
    assert result.returncode == 0, result.stdout + result.stderr


def test_import_contract_check_rejects_a_real_violation(tmp_path) -> None:
    config = tmp_path / "imports.ini"
    config.write_text(
        "[importlinter]\nroot_package = envoy\ninclude_external_packages = True\n"
        "[importlinter:contract:broken]\nname = Reject domain to pydantic\n"
        "type = forbidden\nsource_modules = envoy.domain\nforbidden_modules = pydantic\n"
    )
    result = run_contracts(str(config))
    assert result.returncode != 0, result.stdout
