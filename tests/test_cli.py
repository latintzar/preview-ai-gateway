from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from gateway.cli import private_environment


def test_private_environment_is_owned_private_literal_and_allowlisted(tmp_path):
    private = tmp_path / "operator.env"
    marker = tmp_path / "shell-was-executed"
    value = "$(touch " + str(marker) + ")"
    private.write_text("ATELIER_OPENAI_API_KEY='" + value + "'\nATELIER_SHARED_AI_ENABLED=false\n")
    private.chmod(0o600)
    assert private_environment(private)["ATELIER_OPENAI_API_KEY"] == value
    assert not marker.exists()
    private.chmod(0o644)
    with pytest.raises(ValueError):
        private_environment(private)
    private.chmod(0o600)
    linked = tmp_path / "link.env"
    linked.symlink_to(private)
    with pytest.raises(ValueError):
        private_environment(linked)
    hard = tmp_path / "hard.env"
    os.link(private, hard)
    with pytest.raises(ValueError):
        private_environment(private)


@pytest.mark.parametrize("text", ["OPENAI_API_KEY=value", "ATELIER_OPENAI_API_KEY=a\nATELIER_OPENAI_API_KEY=b", "export ATELIER_OPENAI_API_KEY=value"])
def test_private_environment_rejects_unapproved_names_and_duplicates(tmp_path, text):
    private = tmp_path / "operator.env"
    private.write_text(text); private.chmod(0o600)
    with pytest.raises(ValueError):
        private_environment(private)


def test_doctor_masks_private_values_and_never_creates_ledger(tmp_path):
    secret = "sk-test-" + "d" * 45
    ledger = tmp_path / "private" / "ledger.sqlite3"
    private = tmp_path / "operator.env"
    private.write_text("ATELIER_OPENAI_API_KEY=" + secret + "\nATELIER_SHARED_AI_ENABLED=false\nATELIER_LEDGER_PATH=" + str(ledger) + "\n")
    private.chmod(0o600)
    result = subprocess.run([sys.executable, "-m", "gateway.cli", "doctor", "--env-file", str(private)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)
    assert secret not in result.stdout + result.stderr
    assert '"personal_key_configured": true' in result.stdout
    assert '"shared_enabled": false' in result.stdout and '"provider_calls": 0' in result.stdout
    assert not ledger.exists()


def test_doctor_configuration_failure_does_not_echo_values(tmp_path):
    secret = "sk-test-" + "e" * 45
    private = tmp_path / "operator.env"
    private.write_text("ATELIER_DAILY_LIMIT_USD=" + secret + "\n")
    private.chmod(0o600)
    result = subprocess.run([sys.executable, "-m", "gateway.cli", "doctor", "--env-file", str(private)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 2
    assert secret not in result.stdout + result.stderr and "Traceback" not in result.stderr
