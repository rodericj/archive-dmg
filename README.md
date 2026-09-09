# archive-dmg

Archive macOS DMG files to Amazon S3, where a bucket lifecycle rule later
transitions them to Glacier Deep Archive for long-term, low-cost storage.

## What it does

- Verifies a `.dmg` file's integrity (`hdiutil verify`), mounts it read-only,
  and inspects its contents (file count, top-level entries, modification
  date range).
- Computes a SHA-256 checksum and writes a standard `.sha256` companion file.
- Writes a small archival manifest (`.manifest.json`) recording just enough
  to locate and verify the archive later.
- Uploads the DMG, checksum file, and manifest to S3 with a progress bar,
  automatic multipart transfer for large files, and post-upload verification
  of the remote object.
- Lists what is already archived, showing each object's storage class and
  whether it can be read right now or needs a Glacier restore first.
- Downloads an archive back, requesting the Deep Archive restore when needed
  and verifying the result against the `.sha256` stored beside it in S3.
- Runs a `doctor` command that checks your local environment and AWS setup
  before you try to upload anything.

## What it intentionally does not do

archive-dmg preserves and verifies DMGs. It is not a digital asset manager.
It does **not**:

- Extract or catalog EXIF metadata
- Guess which camera produced a file
- Categorize photos by lens, location, or event
- Perform facial recognition or media organization
- Build a photo-management database

A DMG containing files from multiple cameras is expected and not treated as
an error. Use Lightroom, Apple Photos, or another DAM for organization --
archive-dmg's only job is getting the DMG safely into S3 and letting you
confirm later that what comes back out matches what went in.

## Requirements

- macOS (uses `hdiutil` to verify, mount, and inspect DMGs)
- Python 3.11+
- An AWS account with credentials configured (any provider boto3 supports:
  a profile, `AWS_PROFILE`, environment variables, IAM access keys, AWS SSO,
  or an assumed role)
- An S3 bucket that already exists. This project assumes the bucket already
  has versioning enabled, default (SSE-S3) encryption, all public access
  blocked, a lifecycle rule transitioning current and noncurrent objects to
  `DEEP_ARCHIVE`, and a rule that aborts incomplete multipart uploads. Run
  `archive-dmg doctor` to confirm.

## Installation

Install with [pipx](https://pipx.pypa.io) (recommended) or
[uv](https://docs.astral.sh/uv/):

```sh
pipx install -e .
```

or:

```sh
uv tool install -e .
```

Both give you an `archive-dmg` command on your `PATH`. `-e` (editable) is
recommended while the project lives as a local checkout; drop it for a
standard install once you build/publish a wheel.

## Configuration

archive-dmg reads `~/.config/archive-dmg/config.toml` by default:

```toml
bucket = "roderic-campbell-photo-archive"
region = "us-west-2"
default_prefix = "card-archives"
```

Create a starter file with:

```sh
archive-dmg config init --bucket my-bucket --region us-west-2 --prefix card-archives
```

Inspect the fully resolved configuration (after CLI overrides) with:

```sh
archive-dmg config show
```

**Precedence**, highest first: CLI argument (`--bucket`, `--region`,
`--prefix`, `--profile`, `--config`) > config file > built-in default. There
is no built-in default for `bucket` or `region` -- one of the config file or
`--bucket`/`--region` must supply them. `default_prefix` defaults to an empty
string (upload directly under the bucket root) when nothing else sets it.

## `doctor`

Run this before your first upload, and any time something seems off:

```sh
archive-dmg doctor
```

```text
Checking environment...

✓ Python 3.13.0
✓ macOS
✓ hdiutil
✓ shasum or Python SHA-256 support
✓ AWS CLI
    aws-cli/2.15.0 Python/3.11.6 Darwin/23.0.0 source/arm64

Checking AWS...

✓ Credentials are usable
    arn:aws:iam::123456789012:user/photo-archive
✓ Bucket exists
    roderic-campbell-photo-archive
✓ Region
    us-west-2
✓ Versioning enabled
✓ Default encryption enabled
✓ Public access blocked
✓ Deep Archive lifecycle rule found
✓ Incomplete multipart uploads are cleaned up

Everything looks good.
```

Checks are split into blocking failures (missing `hdiutil`, bad credentials,
missing bucket -- exits nonzero) and warnings (missing AWS CLI, bucket
hygiene settings that are recommended but not required for an upload to
succeed). `doctor` uses `sts:GetCallerIdentity` for the authentication check,
so it works with an IAM user, SSO, or an assumed role -- it does not assume
any particular credential type.

## `verify`

Check a DMG's integrity and contents without uploading anything:

```sh
archive-dmg verify "Camera Card 01.dmg"
```

```text
Verifying Camera Card 01.dmg

✓ DMG checksums verified
✓ Mounted read-only

Files
    2873

Top-level entries
    DCIM, FFDB, PRIVATE, UPD

Earliest file
    2025-02-22T18:14:03Z

Latest file
    2026-06-22T12:05:44Z

✓ Detached cleanly
```

`verify` mounts the DMG read-only with `-nobrowse` (so it never opens in
Finder), detects the mount point from `hdiutil`'s structured plist output,
and always detaches -- including if verification fails partway through.
Normal macOS metadata (`.Spotlight-V100`, `.fseventsd`, `.DS_Store`, etc.) is
ignored when counting files and listing top-level entries. `DCIM` is noted
if present but is never required -- other archive layouts are valid too.

## `upload`

```sh
archive-dmg upload "Camera Card 01.dmg" --prefix "2026/card-archives"
```

```text
Archiving Camera Card 01.dmg

Checking environment
✓ AWS credentials
✓ Bucket reachable

Verifying image
✓ DMG checksums verified
✓ Mounted read-only

Archive contents
Files              2,873
Top-level entries  DCIM, FFDB, PRIVATE, UPD
Date range         2025-02-22 → 2026-06-22

Calculating SHA-256
✓ 8f14e45fceea167a5a36dedd4bea2543...

Uploading
Camera Card 01.dmg          ████████████████████ 100%
Checksum file                ✓
Manifest                     ✓

Verifying S3 object
✓ Remote object exists
✓ Size matches
✓ Checksum verified

Success

Bucket
    roderic-campbell-photo-archive

Object
    2026/card-archives/Camera Card 01.dmg

Size
    74.5 GB

SHA-256
    8f14e45fceea167a5a36dedd4bea2543...

Lifecycle
    Scheduled to transition to Glacier Deep Archive per the bucket's lifecycle rule
```

The pipeline: load config, validate the path, check AWS auth and bucket
reachability, verify the DMG (same code path as `verify`), compute its
SHA-256, upload the DMG (multipart automatically for large files, with a
progress bar and an S3-computed checksum requested via
`ChecksumAlgorithm=SHA256`), upload the `.sha256` file, verify the uploaded
object (existence, size, and checksum where directly comparable), then build
and upload the final manifest (see below for why the manifest is uploaded
last).

By default, an existing destination object blocks the upload:

```text
The destination object already exists:

    s3://roderic-campbell-photo-archive/2026/card-archives/Camera Card 01.dmg

Use --overwrite only if replacing it is intentional.
```

Pass `--overwrite` to replace it intentionally. Because bucket versioning is
enabled, an overwrite is recoverable, but it still requires explicit intent.

## S3 object layout

```text
s3://roderic-campbell-photo-archive/2026/card-archives/
├── Camera Card 01.dmg
├── Camera Card 01.dmg.sha256
└── Camera Card 01.dmg.manifest.json
```

`--prefix` is just the leading portion of the object key -- S3 has no real
directories, and archive-dmg never creates anything resembling a folder
object.

## Manifest

A minimal, archival-only JSON file, e.g. `Camera Card 01.dmg.manifest.json`:

```json
{
  "schema_version": 1,
  "archive_filename": "Camera Card 01.dmg",
  "archive_size_bytes": 74510368964,
  "archive_sha256": "8f14e45fceea167a5a36dedd4bea2543...",
  "archive_created_utc": "2026-08-05T18:03:22Z",
  "contents": {
    "file_count": 2873,
    "earliest_file_modified_utc": "2025-02-22T18:14:03Z",
    "latest_file_modified_utc": "2026-06-22T12:05:44Z",
    "top_level_entries": ["DCIM", "FFDB", "PRIVATE", "UPD"]
  },
  "destination": {
    "bucket": "roderic-campbell-photo-archive",
    "key": "2026/card-archives/Camera Card 01.dmg",
    "region": "us-west-2"
  },
  "verification": {
    "dmg_checksum_verified": true,
    "mounted_read_only": true,
    "remote_size_verified": true,
    "remote_checksum_verified": true,
    "remote_checksum_status": "verified_direct_match"
  }
}
```

For files large enough to trigger multipart upload, S3's `ChecksumSHA256` is
a checksum computed over the part checksums, not the whole file -- it is not
directly comparable to a plain SHA-256 digest. In that case
`remote_checksum_verified` is `false` and `remote_checksum_status` is
`"stored_by_s3_not_directly_comparable"` rather than claiming a match that
wasn't actually confirmed. Object size is still verified directly either way.

Design note: to keep the manifest accurate, archive-dmg uploads the DMG and
its `.sha256` file first, verifies the uploaded object, *then* builds and
uploads the manifest -- so the `verification` block always reflects what
actually happened, not a guess made before the upload completed.

The manifest never contains camera guesses, lens information, EXIF
summaries, trip assumptions, Lightroom metadata, or a per-file inventory.

## Verifying a downloaded DMG later

```sh
shasum -a 256 -c "Camera Card 01.dmg.sha256"
```

This works against any copy of the DMG -- the one that was uploaded, a copy
restored from Deep Archive, or a copy on a new machine -- as long as the
`.sha256` file sits next to it.

## How the lifecycle transition works

Once uploaded, an object begins in S3 Standard. The bucket's lifecycle rule
(already configured, per this project's assumptions) transitions it to
`DEEP_ARCHIVE` after 30 days. A few things worth knowing:

- The object stays visible in S3 listings the whole time; only its storage
  class changes.
- Once in Deep Archive, the object **cannot be downloaded directly**. You
  must first initiate a restore request (e.g.
  `aws s3api restore-object --bucket ... --key ... --restore-request '{"Days": 7, "GlacierJobParameters": {"Tier": "Standard"}}'`).
  Restore requests are also available for noncurrent (overwritten) versions,
  since versioning is enabled.
- Restoration is not instant -- it can take hours, and the exact duration
  depends on the retrieval tier you request. archive-dmg does not print a
  specific restore time estimate, since that number comes from AWS at
  request time, not from this tool.
- A restored copy is temporary and expires after the number of days you
  requested; the object remains in Deep Archive as the permanent copy.
- `archive-dmg list` shows each object's storage class and whether it is
  readable right now, and `archive-dmg download --restore` requests the
  temporary copy, so neither step needs the AWS CLI.

## `list`

Browse what is already archived, newest first:

```sh
archive-dmg list
archive-dmg list --prefix ''          # the whole bucket
archive-dmg list --all                # include .sha256/.manifest.json companions
```

The `Status` column answers the only question that matters before a download:
whether the object can be read right now. `ready` means a directly readable
storage class, `needs restore` means Glacier storage with no temporary copy,
`restoring...` means AWS is working on one, and `restored until <date>` means
a temporary copy exists and when it expires.

Restore state comes back in the same `ListObjectsV2` call via
`OptionalObjectAttributes`, so listing costs one request per page rather than
a `HeadObject` per object.

## `download`

```sh
archive-dmg download 'dji-session/DJIMiniPro4First3Years.dmg' -o ~/Downloads
```

The download is verified, not just transferred. After the bytes land,
`download` compares the local file's size against S3's metadata, then fetches
the `<key>.sha256` companion, hashes the local file, and compares the two. A
mismatch is an error, not a warning. On success it writes the `.sha256` file
next to the download so the check can be repeated later with `shasum`.

If the object is in Glacier storage with no restored copy, `download` refuses
to start rather than failing partway, and points at the restore step:

```sh
archive-dmg download 'dji-session/DJIMiniPro4First3Years.dmg' --restore
archive-dmg download 'dji-session/DJIMiniPro4First3Years.dmg' --restore --restore-tier Bulk
```

`--restore` requests the temporary copy and exits; it does not wait, because
Deep Archive retrieval takes hours (roughly 12 at `Standard`, up to 48 at
`Bulk`, which is cheaper). Check progress with `archive-dmg list`, then run
`download` again without `--restore`. Requesting a restore for an object that
is already readable is reported as such instead of issuing a pointless
request.

Other options: `--overwrite` to replace an existing local file, `--no-verify`
to skip the checksum comparison, and `--restore-days` to control how long the
temporary copy lasts (default 7).

## Security notes

- Credentials are resolved entirely through boto3's normal provider chain
  (profile, `AWS_PROFILE`, environment variables, IAM access keys, SSO,
  assumed roles). archive-dmg never reads `~/.aws/credentials` itself and
  never logs secrets, access key IDs, tokens, or signed request details.
- Objects are uploaded to a bucket that (per this project's assumptions) has
  default server-side encryption and blocks all public access; `doctor`
  checks both.
- Bucket and region are never hardcoded in application logic -- only in your
  local config file or CLI arguments.

## Development

```sh
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"

ruff format .
ruff check .
mypy src
pytest
```

No test requires live AWS credentials or a real DMG -- AWS calls are tested
with `botocore.stub.Stubber`, and `hdiutil`/mount behavior is tested by
mocking `subprocess.run`.

## Current limitations

- Only already-created `.dmg` files can be archived; there is no SD-card
  imaging yet (`archive-dmg create` is planned -- see Roadmap).
- Only S3-managed (SSE-S3) default bucket encryption is checked by `doctor`;
  SSE-KMS buckets are treated as encrypted but key policy is not inspected.
- `doctor --fix` (automatic remediation) is not implemented by design for
  this first version.
- `list` reads storage class and restore state straight from S3 rather than
  from the uploaded `.manifest.json` files, so it does not yet surface file
  counts or capture date ranges in the listing.

## Roadmap

- `archive-dmg create`: build a DMG from an SD card or mounted volume, then
  call the same verification/checksum/manifest/upload pipeline `upload`
  already uses.
- Use the `.manifest.json` companions as a richer index for `list`, so the
  listing can show file counts and capture date ranges without downloading
  the archive.
