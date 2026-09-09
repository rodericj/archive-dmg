"""Tests for the Typer CLI: argument parsing, exit codes, and error rendering.

No live AWS calls or real DMGs are involved -- these tests exercise argument
parsing, config resolution, and the error-rendering path for validation
failures that happen before any AWS call would occur.
"""

from __future__ import annotations

from typer.testing import CliRunner

from archive_dmg.cli import app

runner = CliRunner()


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.output
    assert "verify" in result.output
    assert "upload" in result.output
    assert "config" in result.output


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "archive-dmg" in result.output


def test_verify_help_documents_argument():
    result = runner.invoke(app, ["verify", "--help"])
    assert result.exit_code == 0
    assert "dmg" in result.output.lower()


def test_verify_missing_file_exits_nonzero_with_helpful_message(tmp_path):
    missing = tmp_path / "missing.dmg"
    result = runner.invoke(app, ["verify", str(missing)])
    assert result.exit_code == 1
    assert "File not found" in result.output


def test_verify_rejects_non_dmg_extension(tmp_path):
    bogus = tmp_path / "archive.zip"
    bogus.write_bytes(b"data")
    result = runner.invoke(app, ["verify", str(bogus)])
    assert result.exit_code == 1
    assert ".dmg" in result.output


def test_verify_rejects_directory(tmp_path):
    result = runner.invoke(app, ["verify", str(tmp_path)])
    assert result.exit_code == 1


def test_upload_without_bucket_or_region_fails_with_config_hint(tmp_path):
    dmg_path = tmp_path / "Card.dmg"
    dmg_path.write_bytes(b"data")
    config_path = tmp_path / "config.toml"

    result = runner.invoke(app, ["upload", str(dmg_path), "--config", str(config_path)])

    assert result.exit_code == 1
    assert "bucket" in result.output.lower()


def test_upload_missing_file_fails_before_any_aws_call(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('bucket = "b"\nregion = "us-west-2"\n')

    result = runner.invoke(
        app,
        ["upload", str(tmp_path / "missing.dmg"), "--config", str(config_path)],
    )

    assert result.exit_code == 1
    assert "File not found" in result.output


def test_doctor_reports_missing_config():
    result = runner.invoke(app, ["doctor", "--config", "/nonexistent/path/config.toml"])
    assert result.exit_code == 1
    assert "environment" in result.output.lower()


def test_config_init_creates_file_and_refuses_overwrite(tmp_path):
    config_path = tmp_path / "config.toml"

    first = runner.invoke(
        app,
        [
            "config",
            "init",
            "--config",
            str(config_path),
            "--bucket",
            "my-bucket",
            "--region",
            "us-west-2",
            "--prefix",
            "cards",
        ],
    )
    assert first.exit_code == 0
    assert config_path.exists()
    assert 'bucket = "my-bucket"' in config_path.read_text()

    second = runner.invoke(app, ["config", "init", "--config", str(config_path)])
    assert second.exit_code == 1
    assert "already exists" in second.output

    third = runner.invoke(app, ["config", "init", "--config", str(config_path), "--force"])
    assert third.exit_code == 0


def test_config_show_reflects_cli_overrides(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('bucket = "file-bucket"\nregion = "us-east-1"\n')

    result = runner.invoke(
        app,
        ["config", "show", "--config", str(config_path), "--bucket", "override-bucket"],
    )

    assert result.exit_code == 0
    assert "override-bucket" in result.output
    assert "us-east-1" in result.output


# --- list / download -----------------------------------------------------------


def test_help_lists_new_read_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "download" in result.output


def test_download_help_documents_restore_workflow():
    result = runner.invoke(app, ["download", "--help"])
    assert result.exit_code == 0
    assert "--restore" in result.output
    assert "Glacier" in result.output


def test_list_help_documents_prefix_and_all():
    result = runner.invoke(app, ["list", "--help"])
    assert result.exit_code == 0
    assert "--prefix" in result.output
    assert "--all" in result.output


def test_list_without_bucket_reports_config_error(tmp_path):
    result = runner.invoke(app, ["list", "--config", str(tmp_path / "absent.toml")])
    assert result.exit_code == 1
    assert "bucket" in result.output.lower()


def test_download_without_bucket_reports_config_error(tmp_path):
    result = runner.invoke(
        app, ["download", "p/Card.dmg", "--config", str(tmp_path / "absent.toml")]
    )
    assert result.exit_code == 1
    assert "bucket" in result.output.lower()


def test_download_rejects_unknown_restore_tier_without_touching_aws(tmp_path):
    """An invalid tier must fail locally, before any credential or network use."""
    result = runner.invoke(
        app,
        [
            "download",
            "p/Card.dmg",
            "--config",
            str(tmp_path / "absent.toml"),
            "--bucket",
            "b",
            "--region",
            "us-west-2",
            "--restore",
            "--restore-tier",
            "Speedy",
        ],
    )
    assert result.exit_code == 1
    assert "Speedy" in result.output
    assert "Standard" in result.output


def test_download_rejects_zero_restore_days_without_touching_aws(tmp_path):
    result = runner.invoke(
        app,
        [
            "download",
            "p/Card.dmg",
            "--config",
            str(tmp_path / "absent.toml"),
            "--bucket",
            "b",
            "--region",
            "us-west-2",
            "--restore",
            "--restore-days",
            "0",
        ],
    )
    assert result.exit_code == 1
    assert "at least 1" in result.output
