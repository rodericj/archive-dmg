"""Tests for archive_dmg.config: loading, precedence, and example-file creation."""

from __future__ import annotations

import pytest

from archive_dmg import config
from archive_dmg.errors import ConfigError


def test_load_config_file_missing_returns_empty(tmp_path):
    values = config.load_config_file(tmp_path / "missing.toml")
    assert values == config.ConfigFileValues()


def test_load_config_file_parses_values(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('bucket = "my-bucket"\nregion = "us-west-2"\ndefault_prefix = "cards"\n')

    values = config.load_config_file(path)

    assert values.bucket == "my-bucket"
    assert values.region == "us-west-2"
    assert values.default_prefix == "cards"


def test_load_config_file_invalid_toml_raises(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("this is not valid toml =")

    with pytest.raises(ConfigError):
        config.load_config_file(path)


def test_load_config_file_wrong_type_raises(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("bucket = 123\n")

    with pytest.raises(ConfigError):
        config.load_config_file(path)


def test_resolve_config_cli_overrides_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('bucket = "file-bucket"\nregion = "us-east-1"\n')

    cfg = config.resolve_config(config_path=path, cli_bucket="cli-bucket")

    assert cfg.bucket == "cli-bucket"
    assert cfg.region == "us-east-1"


def test_resolve_config_prefix_precedence(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('bucket = "b"\nregion = "us-west-2"\ndefault_prefix = "file-prefix"\n')

    assert config.resolve_config(config_path=path, cli_prefix="cli-prefix").default_prefix == (
        "cli-prefix"
    )
    assert config.resolve_config(config_path=path).default_prefix == "file-prefix"


def test_resolve_config_missing_prefix_defaults_to_empty_string(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('bucket = "b"\nregion = "us-west-2"\n')

    assert config.resolve_config(config_path=path).default_prefix == ""


def test_resolve_config_missing_bucket_raises(tmp_path):
    with pytest.raises(ConfigError):
        config.resolve_config(config_path=tmp_path / "missing.toml", cli_region="us-west-2")


def test_resolve_config_missing_region_raises(tmp_path):
    with pytest.raises(ConfigError):
        config.resolve_config(config_path=tmp_path / "missing.toml", cli_bucket="b")


def test_resolve_config_profile_precedence(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('bucket = "b"\nregion = "us-west-2"\nprofile = "file-profile"\n')

    cfg = config.resolve_config(config_path=path, cli_profile="cli-profile")
    assert cfg.profile == "cli-profile"
    assert config.resolve_config(config_path=path).profile == "file-profile"


def test_write_example_config_creates_file(tmp_path):
    path = tmp_path / "nested" / "config.toml"

    written = config.write_example_config(path, bucket="b", region="us-west-2")

    assert written == path
    assert 'bucket = "b"' in path.read_text()
    assert 'region = "us-west-2"' in path.read_text()


def test_write_example_config_refuses_to_overwrite(tmp_path):
    path = tmp_path / "config.toml"
    config.write_example_config(path)

    with pytest.raises(ConfigError):
        config.write_example_config(path)


def test_write_example_config_force_overwrites(tmp_path):
    path = tmp_path / "config.toml"
    config.write_example_config(path, bucket="first")

    config.write_example_config(path, bucket="second", force=True)

    assert "second" in path.read_text()
    assert "first" not in path.read_text()
