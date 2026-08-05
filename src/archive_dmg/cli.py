"""Typer CLI entry point: wires commands to the service layer and renders output.

This module owns all Rich printing and Typer argument parsing. Every other
module is either Rich-free (``dmg.py``, ``aws.py``, ``doctor.py``,
``checksum.py``, ``manifest.py``, ``archive_service.py``) or a pure rendering
helper (``ui.py``) with no Typer/argument-parsing knowledge, so the business
logic can be tested without a terminal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import Progress, TaskID

from archive_dmg import __version__
from archive_dmg.archive_service import UploadReporter, upload_archive
from archive_dmg.config import DEFAULT_CONFIG_PATH, resolve_config, write_example_config
from archive_dmg.dmg import validate_dmg_path, verify_dmg
from archive_dmg.doctor import run_aws_checks, run_environment_checks
from archive_dmg.errors import ArchiveDmgError
from archive_dmg.models import CheckResult, DmgVerificationResult, RemoteVerificationResult
from archive_dmg.ui import (
    create_upload_progress,
    format_bytes,
    format_count,
    format_date_range,
    format_entries,
    format_timestamp,
    print_check,
    print_error,
    print_key_value,
    print_section,
    print_success,
)

app = typer.Typer(
    name="archive-dmg",
    help=(
        "Archive macOS DMG files to Amazon S3, where a lifecycle rule transitions them "
        "to Glacier Deep Archive. Preserves and verifies the DMG -- it is not a photo "
        "manager."
    ),
    no_args_is_help=True,
    add_completion=True,
    rich_markup_mode="rich",
)
config_app = typer.Typer(help="Manage the archive-dmg configuration file.", no_args_is_help=True)
app.add_typer(config_app, name="config")

console = Console()
error_console = Console(stderr=True)

ConfigPathOption = Annotated[
    Path,
    typer.Option("--config", help="Path to the archive-dmg config file.", show_default=True),
]
BucketOption = Annotated[
    str | None, typer.Option("--bucket", help="S3 bucket, overriding the config file.")
]
RegionOption = Annotated[
    str | None, typer.Option("--region", help="AWS region, overriding the config file.")
]
ProfileOption = Annotated[
    str | None, typer.Option("--profile", help="AWS profile to use (see AWS_PROFILE).")
]


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"archive-dmg {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Archive macOS DMG files to Amazon S3 for eventual Glacier Deep Archive storage."""


def _checksum_status_message(status: str) -> str:
    if status == "stored_by_s3_not_directly_comparable":
        return (
            "S3 stored a composite checksum because this upload used multipart transfer. "
            "That value cannot be directly compared to a whole-file SHA-256, so only size "
            "was used to confirm the upload. The local .sha256 file remains the source of "
            "truth for integrity verification."
        )
    if status == "not_available":
        return "S3 did not return a comparable checksum for this object."
    return status


class RichUploadReporter(UploadReporter):
    """Drives the Rich output for `upload`, matching each pipeline stage."""

    def __init__(self, rich_console: Console, dmg_name: str) -> None:
        self._console = rich_console
        self._dmg_name = dmg_name
        self._progress: Progress | None = None
        self._task_id: TaskID | None = None

    def environment_checked(self) -> None:
        print_section(self._console, "Checking environment")
        print_check(self._console, CheckResult(name="AWS credentials", status="ok"))
        print_check(self._console, CheckResult(name="Bucket reachable", status="ok"))

    def verification_complete(self, result: DmgVerificationResult) -> None:
        print_section(self._console, "Verifying image")
        print_check(self._console, CheckResult(name="DMG checksums verified", status="ok"))
        print_check(self._console, CheckResult(name="Mounted read-only", status="ok"))

        contents = result.contents
        print_section(self._console, "Archive contents")
        self._console.print(f"Files              {format_count(contents.file_count)}")
        self._console.print(f"Top-level entries  {format_entries(contents.top_level_entries)}")
        date_range = format_date_range(
            contents.earliest_file_modified_utc, contents.latest_file_modified_utc
        )
        self._console.print(f"Date range         {date_range}")

    def checksum_ready(self, sha256_hex: str) -> None:
        print_section(self._console, "Calculating SHA-256")
        print_check(self._console, CheckResult(name=sha256_hex, status="ok"))

    def upload_started(self, total_bytes: int) -> None:
        print_section(self._console, "Uploading")
        self._progress = create_upload_progress(self._console)
        self._progress.start()
        self._task_id = self._progress.add_task(self._dmg_name, total=total_bytes)

    def upload_progress(self, bytes_transferred: int) -> None:
        if self._progress is not None and self._task_id is not None:
            self._progress.update(self._task_id, advance=bytes_transferred)

    def upload_dmg_complete(self) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None

    def upload_sha256_complete(self) -> None:
        self._console.print("Checksum file                [green]✓[/green]")

    def upload_manifest_complete(self) -> None:
        self._console.print("Manifest                     [green]✓[/green]")

    def remote_verification_complete(self, result: RemoteVerificationResult) -> None:
        print_section(self._console, "Verifying S3 object")
        print_check(self._console, CheckResult(name="Remote object exists", status="ok"))
        print_check(self._console, CheckResult(name="Size matches", status="ok"))
        if result.remote_checksum_verified:
            print_check(self._console, CheckResult(name="Checksum verified", status="ok"))
        else:
            print_check(
                self._console,
                CheckResult(
                    name="Checksum comparison limited",
                    status="warn",
                    detail=_checksum_status_message(result.remote_checksum_status),
                ),
            )


@app.command()
def doctor(
    config_path: ConfigPathOption = DEFAULT_CONFIG_PATH,
    bucket: BucketOption = None,
    region: RegionOption = None,
    profile: ProfileOption = None,
) -> None:
    """Check that the local environment and AWS are ready for archiving."""
    console.print("Checking environment...")
    console.print()
    env_checks = run_environment_checks()
    for check in env_checks:
        print_check(console, check)

    console.print()
    console.print("Checking AWS...")
    console.print()

    try:
        cfg = resolve_config(
            config_path=config_path,
            cli_bucket=bucket,
            cli_region=region,
            cli_profile=profile,
        )
    except ArchiveDmgError as exc:
        print_error(console, exc)
        raise typer.Exit(code=1) from None

    aws_checks = run_aws_checks(profile=cfg.profile, bucket=cfg.bucket, region=cfg.region)
    for check in aws_checks:
        print_check(console, check)

    if any(check.status == "fail" for check in (*env_checks, *aws_checks)):
        console.print()
        console.print("Some checks failed. See details above.", style="bold red")
        raise typer.Exit(code=1)

    console.print()
    print_success(console, "Everything looks good.")


@app.command()
def verify(
    dmg_path: Annotated[Path, typer.Argument(help="Path to the .dmg file to verify.")],
) -> None:
    """Verify a DMG's integrity and inspect its contents, without uploading anything."""
    try:
        path = validate_dmg_path(dmg_path)
        console.print(f"Verifying {path.name}")
        result = verify_dmg(path)
    except ArchiveDmgError as exc:
        print_error(error_console, exc)
        raise typer.Exit(code=exc.exit_code) from None

    console.print()
    print_check(console, CheckResult(name="DMG checksums verified", status="ok"))
    print_check(console, CheckResult(name="Mounted read-only", status="ok"))

    contents = result.contents
    print_key_value(console, "Files", format_count(contents.file_count))
    print_key_value(console, "Top-level entries", format_entries(contents.top_level_entries))
    print_key_value(console, "Earliest file", format_timestamp(contents.earliest_file_modified_utc))
    print_key_value(console, "Latest file", format_timestamp(contents.latest_file_modified_utc))
    if contents.has_dcim:
        console.print()
        console.print("DCIM directory found.", style="dim")

    console.print()
    print_check(console, CheckResult(name="Detached cleanly", status="ok"))


@app.command()
def upload(
    dmg_path: Annotated[Path, typer.Argument(help="Path to the .dmg file to archive.")],
    config_path: ConfigPathOption = DEFAULT_CONFIG_PATH,
    bucket: BucketOption = None,
    region: RegionOption = None,
    prefix: Annotated[
        str | None, typer.Option("--prefix", help="S3 key prefix, overriding the config file.")
    ] = None,
    profile: ProfileOption = None,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite", help="Allow replacing an existing S3 object at the destination."
        ),
    ] = False,
) -> None:
    """Verify, checksum, and upload a DMG to S3, then confirm the upload succeeded."""
    try:
        cfg = resolve_config(
            config_path=config_path,
            cli_bucket=bucket,
            cli_region=region,
            cli_prefix=prefix,
            cli_profile=profile,
        )
        console.print(f"Archiving {dmg_path.name}")
        reporter = RichUploadReporter(console, dmg_path.name)
        result = upload_archive(
            dmg_path=dmg_path, config=cfg, overwrite=overwrite, reporter=reporter
        )
    except ArchiveDmgError as exc:
        print_error(error_console, exc)
        raise typer.Exit(code=exc.exit_code) from None
    except KeyboardInterrupt:
        error_console.print()
        error_console.print("Interrupted.", style="bold yellow")
        raise typer.Exit(code=130) from None

    print_success(console, "Success")
    print_key_value(console, "Bucket", result.destination.bucket)
    print_key_value(console, "Object", result.destination.key)
    print_key_value(console, "Size", format_bytes(result.archive_size_bytes))
    print_key_value(console, "SHA-256", result.archive_sha256)
    print_key_value(
        console,
        "Lifecycle",
        "Scheduled to transition to Glacier Deep Archive per the bucket's lifecycle rule "
        "(run 'archive-dmg doctor' to confirm one is configured).",
    )


@config_app.command("init")
def config_init(
    config_path: ConfigPathOption = DEFAULT_CONFIG_PATH,
    bucket: Annotated[
        str, typer.Option("--bucket", help="Bucket name to write into the config.")
    ] = ("your-bucket-name"),
    region: Annotated[str, typer.Option("--region", help="Region to write into the config.")] = (
        "us-west-2"
    ),
    prefix: Annotated[
        str, typer.Option("--prefix", help="Default key prefix to write into the config.")
    ] = "card-archives",
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing config file.")
    ] = False,
) -> None:
    """Create a starter config file at the default (or given) location."""
    try:
        written_path = write_example_config(
            config_path, bucket=bucket, region=region, default_prefix=prefix, force=force
        )
    except ArchiveDmgError as exc:
        print_error(error_console, exc)
        raise typer.Exit(code=exc.exit_code) from None

    print_success(console, f"Wrote config file: {written_path}")
    console.print()
    console.print("Edit it to set your bucket, region, and default prefix.")


@config_app.command("show")
def config_show(
    config_path: ConfigPathOption = DEFAULT_CONFIG_PATH,
    bucket: BucketOption = None,
    region: RegionOption = None,
    prefix: Annotated[str | None, typer.Option("--prefix", help="S3 key prefix override.")] = None,
    profile: ProfileOption = None,
) -> None:
    """Show the fully resolved configuration (after applying CLI overrides)."""
    try:
        cfg = resolve_config(
            config_path=config_path,
            cli_bucket=bucket,
            cli_region=region,
            cli_prefix=prefix,
            cli_profile=profile,
        )
    except ArchiveDmgError as exc:
        print_error(error_console, exc)
        raise typer.Exit(code=exc.exit_code) from None

    print_key_value(console, "Config file", str(config_path))
    print_key_value(console, "Bucket", cfg.bucket)
    print_key_value(console, "Region", cfg.region)
    print_key_value(console, "Default prefix", cfg.default_prefix or "(none)")
    print_key_value(console, "Profile", cfg.profile or "(default)")


if __name__ == "__main__":
    app()
