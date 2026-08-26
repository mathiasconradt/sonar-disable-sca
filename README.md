# sonar_disable_sca.py

Disable SonarQube Cloud SCA by default for multiple organizations — either listed in a CSV file, or auto-inventoried from every organization attached to a SonarQube Cloud Enterprise.

This script is meant for SonarQube Cloud only. It is not for SonarQube Server.

![sonar_disable_sca usage screenshot](screenshot.png)

## Requirements

- Python 3
- No external Python packages
- `SONAR_TOKEN` environment variable set
- For CSV input: token must belong to a user with organization admin rights for every target org
- For `--enterprise-key` input: token must belong to an **Enterprise Administrator** of that Enterprise

Project analysis tokens are not enough. A `403 Insufficient privileges` response means the token is valid, but the user lacks org admin permission for that organization.

## Choosing an Org-Key Source

Pick one:

- **CSV file** (default) — a manually curated list of org keys.
- **`--enterprise-key KEY`** — auto-inventory every organization attached to that SonarQube Cloud Enterprise. Requires the token to be an Enterprise Administrator for it.

### CSV Input

Default file: `orgs.csv` in the current working directory (prompted interactively), or pass `--csv PATH` to skip the prompt.

Format:

```csv
org-key-1
org-key-2
org-key-3
```

Rules:

- One organization key per row
- No header
- Empty rows ignored
- Surrounding whitespace ignored
- Duplicate org keys removed while preserving first-seen order

### Enterprise Input

```sh
python3 ./sonar_disable_sca.py --enterprise-key my-enterprise ...
```

Resolves the Enterprise, lists every attached organization, and resolves each one to its org key. This talks to a different SonarQube Cloud API surface than the CSV flow:

- CSV flow / disable action: `https://sonarcloud.io/api/...`, HTTP Basic Auth (`SONAR_TOKEN` as username, empty password).
- Enterprise inventory (`--enterprise-key`, and the org lookups it triggers): `https://api.sonarcloud.io/...`, HTTP Bearer Auth (`SONAR_TOKEN` as the bearer token).

Both use the same `SONAR_TOKEN`, just presented differently — nothing extra to configure.

Being an Enterprise Administrator does **not** guarantee membership or admin rights on every organization the Enterprise contains — you may see per-organization `403` errors below even though the Enterprise-level inventory succeeded.

## Read-Only Modes

Two ways to inspect before touching anything — neither makes API writes:

### `--dump-csv PATH`

Requires `--enterprise-key`. Inventories the Enterprise's organizations and writes their keys to `PATH` (same one-key-per-row format as CSV input), then exits. Nothing is disabled. Use this to review or edit the list before running the real action with `--csv PATH`.

```sh
python3 ./sonar_disable_sca.py --enterprise-key my-enterprise --dump-csv orgs.csv
```

### `--list-status`

Works with either org-key source (`--enterprise-key` or `--csv`/CSV prompt). For each organization, checks whether the token has admin rights on it and reports the current `sonar.sca.enabled` value, then exits. Nothing is changed.

```sh
python3 ./sonar_disable_sca.py --enterprise-key my-enterprise --list-status
```

```text
ORGANIZATION                             ADMIN   SONAR.SCA.ENABLED
------------------------------------------------------------------
my-org-one                               yes     true
my-org-two                               no      ERROR: HTTP 403: {"errors":[{"msg":"You're not member of organization 'my-org-two'"}]}
```

Exit code is `1` if any organization errored (not found, no permission, etc.), even though this mode makes no changes — treat it as a diagnostic, not a mutation indicator.

## Run

PowerShell:

```powershell
$env:SONAR_TOKEN = "your-token"
python .\sonar_disable_sca.py
```

Windows CMD:

```cmd
set SONAR_TOKEN=your-token
python sonar_disable_sca.py
```

macOS/Linux:

```sh
export SONAR_TOKEN="your-token"
python3 ./sonar_disable_sca.py
```

Dry run (still asks for confirmation, makes no API calls):

```sh
python3 ./sonar_disable_sca.py --dry-run
```

Full enterprise-driven workflow — inventory, review, then act:

```sh
python3 ./sonar_disable_sca.py --enterprise-key my-enterprise --dump-csv orgs.csv
# review/edit orgs.csv here if needed
python3 ./sonar_disable_sca.py --csv orgs.csv --dry-run
python3 ./sonar_disable_sca.py --csv orgs.csv
```

## Prompts

If `SONAR_TOKEN` exists, script shows masked token only:

```text
SONAR_TOKEN found: abcdef**************uvwxyz
```

Press Enter to use env token, or paste another token to override it for this run.

CSV prompt (skipped if `--csv` or `--enterprise-key` is given):

```text
CSV path relative to current directory [orgs.csv]:
```

Press Enter to use `orgs.csv`, or type another CSV path.

Final confirmation (skipped by `--dump-csv` and `--list-status`, which never prompt):

```text
Continue and disable SCA by default for these organizations? [y/N]:
```

In `--dry-run`, the same prompt is worded to make clear no change will follow:

```text
Continue and preview disabling SCA by default for these organizations (dry-run, no changes will be made)? [y/N]:
```

Press `y` or `Y` to proceed. Any other key cancels with no changes.

## API Action

For each org, the disable action sends:

```text
POST https://sonarcloud.io/api/settings/set
organization=<ORG_KEY>
key=sonar.sca.enabled
value=false
```

Authentication uses HTTP Basic Auth with `SONAR_TOKEN` as username and empty password.

## Exit Codes

- `0`: all requests succeeded, dry run succeeded, `--dump-csv` wrote its file, or user cancelled
- Non-zero: missing token, invalid/empty org-key source, an enterprise/organization lookup failed, or one or more API calls failed (including `--list-status` reporting any per-org error)

## Troubleshooting

- `HTTP 403 Insufficient privileges`: token user lacks org admin rights for that org
- `HTTP 403 ... not member of organization`: seen with `--enterprise-key` / `--list-status` — Enterprise Administrator role doesn't imply membership on every attached org
- `enterprise '<key>' not found, or token lacks the Enterprise Administrator role for it`: check the key and the token's Enterprise role
- `SONAR_TOKEN environment variable is missing`: set env var before running
- CSV parse error: ensure one org key per row, no extra comma-separated columns

## Tests

Offline, stdlib-only test suite covering the read-only paths (`--list-status`, `--dump-csv`, `--dry-run`, and `--csv` as an input source for either) — no network access or real token required; every simulated write is asserted to never happen.

```sh
python3 -m unittest test_sonar_disable_sca.py -v
```
