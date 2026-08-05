"""Configuration loading and precedence rules.

Precedence, highest first: CLI argument > config file > built-in default.
There is no sensible built-in default for ``bucket`` or ``region`` (they are
account-specific), so those must come from the config file or the CLI. The
prefix defaults to an empty string when nothing else is supplied.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from archive_dmg.errors import ConfigError
from archive_dmg.models import ArchiveConfig

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "archive-dmg" / "config.toml"

_EXAMPLE_CONFIG_TEMPLATE = """\
# archive-dmg configuration
# See: archive-dmg --help

# S3 bucket that stores archived DMGs. Must already exist.
bucket = "{bucket}"

# AWS region the bucket lives in.
region = "{region}"

# Default S3 key prefix applied when --prefix is not given on the command
# line. Leave empty ("") to upload directly under the bucket root.
default_prefix = "{default_prefix}"
"""


@dataclass(frozen=True, slots=True)
class ConfigFileValues:
    """Raw values read from the TOML config file, before CLI overrides."""

    bucket: str | None = None
    region: str | None = None
    default_prefix: str | None = None
    profile: str | None = None


def load_config_file(path: Path) -> ConfigFileValues:
    """Read and validate the TOML config file at ``path``.

    A missing file is not an error -- it simply yields empty values, since
    the CLI may supply everything needed via arguments.
    """
    if not path.exists():
        return ConfigFileValues()

    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Could not parse config file: {path}",
            hint=f"Fix the TOML syntax error: {exc}",
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"Could not read config file: {path}",
            hint=str(exc),
        ) from exc

    for key in ("bucket", "region", "default_prefix", "profile"):
        if key in raw and not isinstance(raw[key], str):
            raise ConfigError(
                f"Config value '{key}' in {path} must be a string.",
                hint=f'Example: {key} = "value"',
            )

    return ConfigFileValues(
        bucket=raw.get("bucket"),
        region=raw.get("region"),
        default_prefix=raw.get("default_prefix"),
        profile=raw.get("profile"),
    )


def resolve_config(
    *,
    config_path: Path,
    cli_bucket: str | None = None,
    cli_region: str | None = None,
    cli_prefix: str | None = None,
    cli_profile: str | None = None,
) -> ArchiveConfig:
    """Merge CLI overrides with the config file, per the documented precedence."""
    file_values = load_config_file(config_path)

    bucket = cli_bucket or file_values.bucket
    region = cli_region or file_values.region
    prefix = cli_prefix if cli_prefix is not None else (file_values.default_prefix or "")
    profile = cli_profile or file_values.profile

    if not bucket:
        raise ConfigError(
            "No S3 bucket configured.",
            hint=(
                f"Set 'bucket' in {config_path} or pass --bucket. "
                "Run 'archive-dmg config init' to create a starter config file."
            ),
        )
    if not region:
        raise ConfigError(
            "No AWS region configured.",
            hint=(
                f"Set 'region' in {config_path} or pass --region. "
                "Run 'archive-dmg config init' to create a starter config file."
            ),
        )

    return ArchiveConfig(bucket=bucket, region=region, default_prefix=prefix, profile=profile)


def write_example_config(
    path: Path,
    *,
    bucket: str = "your-bucket-name",
    region: str = "us-west-2",
    default_prefix: str = "card-archives",
    force: bool = False,
) -> Path:
    """Write a starter config file, refusing to clobber an existing one."""
    if path.exists() and not force:
        raise ConfigError(
            f"Config file already exists: {path}",
            hint="Pass --force to overwrite it.",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _EXAMPLE_CONFIG_TEMPLATE.format(
        bucket=bucket, region=region, default_prefix=default_prefix
    )
    path.write_text(content)
    return path
