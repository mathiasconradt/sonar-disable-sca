#!/usr/bin/env python3
r"""
Disable SonarQube Cloud SCA by default for organizations listed in orgs.csv,
or for every organization attached to a SonarQube Cloud Enterprise.

This script is for SonarQube Cloud only, not SonarQube Server.

Input (choose one):
    orgs.csv in the current working directory, one organization key per row, no header.
    --enterprise-key KEY, using an Enterprise Administrator token to inventory
        every organization attached to that Enterprise.

Usage examples:
    PowerShell:
        $env:SONAR_TOKEN = "your-token"
        python .\disable_sca.py

    Windows CMD:
        set SONAR_TOKEN=your-token
        python disable_sca.py

    macOS/Linux shells:
        export SONAR_TOKEN="your-token"
        python3 ./disable_sca.py

    Dry-run mode:
        python3 ./disable_sca.py --dry-run

    Inventory an Enterprise's organizations, save them, don't act:
        python3 ./disable_sca.py --enterprise-key my-enterprise --dump-csv orgs.csv

    Check admin permission and current SCA status without changing anything:
        python3 ./disable_sca.py --enterprise-key my-enterprise --list-status
"""

from __future__ import annotations

import argparse
import base64
import csv
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


V1_BASE_URL = "https://sonarcloud.io/api"
V2_BASE_URL = "https://api.sonarcloud.io"
API_URL = f"{V1_BASE_URL}/settings/set"
DEFAULT_CSV_PATH = "orgs.csv"
SETTING_KEY = "sonar.sca.enabled"
SETTING_VALUE = "false"
TIMEOUT_SECONDS = 30
ERROR_BODY_LIMIT = 800
ORG_IDS_PER_REQUEST = 100
USER_AGENT = "disable-sca-org-default/1.0 (Python urllib; standard-library)"


@dataclass(frozen=True)
class Result:
    organization: str
    ok: bool
    message: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Disable SonarQube Cloud SCA by default for orgs in orgs.csv "
        "or for every organization attached to a SonarQube Cloud Enterprise."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse, preview, and confirm, but apply no changes.",
    )
    parser.add_argument(
        "--enterprise-key",
        metavar="KEY",
        help="Inventory organization keys from this SonarQube Cloud Enterprise "
        "instead of reading a CSV file. Requires an Enterprise Administrator token.",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        help="CSV file to read organization keys from, skipping the interactive "
        "prompt. Ignored if --enterprise-key is set.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dump-csv",
        metavar="PATH",
        help="Write the organizations inventoried via --enterprise-key to PATH "
        "and exit. Makes no other changes. Requires --enterprise-key.",
    )
    mode_group.add_argument(
        "--list-status",
        action="store_true",
        help="Print each organization's admin permission and current "
        f"{SETTING_KEY} value, then exit. Makes no changes.",
    )

    args = parser.parse_args()
    if args.dump_csv and not args.enterprise_key:
        parser.error("--dump-csv requires --enterprise-key")
    if args.csv and args.enterprise_key:
        parser.error("--csv and --enterprise-key are mutually exclusive")
    return args


def read_org_keys(path: str, *, from_flag: bool = False) -> list[str]:
    try:
        with open(path, newline="", encoding="utf-8-sig") as csv_file:
            reader = csv.reader(csv_file)
            ordered_unique: list[str] = []
            seen: set[str] = set()

            for line_number, row in enumerate(reader, start=1):
                values = [cell.strip() for cell in row]

                if not values or all(value == "" for value in values):
                    continue

                if len(values) != 1 or values[0] == "":
                    raise ValueError(
                        f"{path}:{line_number}: expected exactly one organization key"
                    )

                org_key = values[0]
                if org_key not in seen:
                    seen.add(org_key)
                    ordered_unique.append(org_key)

            return ordered_unique
    except FileNotFoundError as exc:
        if from_flag:
            raise ValueError(f"--csv file '{path}' not found") from exc
        raise ValueError(f"{path} not found in current directory") from exc
    except csv.Error as exc:
        raise ValueError(f"failed to parse {path}: {exc}") from exc


def require_token() -> str:
    token = os.environ.get("SONAR_TOKEN")
    if not token:
        print("SONAR_TOKEN environment variable is missing; no changes were made.", file=sys.stderr)
        sys.exit(1)

    print(f"SONAR_TOKEN found: {mask_token(token)}")
    override = getpass.getpass(
        "SONAR_TOKEN is set. Press Enter to use it, or enter a different token to override: "
    ).strip()
    if override:
        return override

    return token


def mask_token(token: str) -> str:
    if len(token) <= 12:
        return "*" * len(token)
    return f"{token[:6]}{'*' * (len(token) - 12)}{token[-6:]}"


def get_csv_path() -> str:
    csv_path = input(
        f"CSV path relative to current directory [{DEFAULT_CSV_PATH}]: "
    ).strip()
    return csv_path or DEFAULT_CSV_PATH


def confirm(org_keys: list[str], dry_run: bool) -> bool:
    print(f"Total unique organizations: {len(org_keys)}")
    print("Organization keys: " + ", ".join(org_keys))
    if dry_run:
        prompt = "Continue and preview disabling SCA by default for these organizations (dry-run, no changes will be made)? [y/N]: "
    else:
        prompt = "Continue and disable SCA by default for these organizations? [y/N]: "
    print(prompt, end="", flush=True)
    answer = read_single_key()
    print(answer)
    return answer.lower() == "y"


def read_single_key() -> str:
    if os.name == "nt":
        import msvcrt

        key = msvcrt.getwch()
        if key in ("\x00", "\xe0"):
            msvcrt.getwch()
            return "n"
        return key

    if not sys.stdin.isatty():
        return sys.stdin.read(1) or "n"

    import termios
    import tty

    file_descriptor = sys.stdin.fileno()
    previous_settings = termios.tcgetattr(file_descriptor)
    try:
        tty.setraw(file_descriptor)
        return sys.stdin.read(1) or "n"
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, previous_settings)


def basic_auth_header(token: str) -> str:
    credentials = f"{token}:".encode("utf-8")
    return f"Basic {base64.b64encode(credentials).decode('ascii')}"


def build_request(org_key: str, token: str) -> urllib.request.Request:
    form_data = urllib.parse.urlencode(
        {
            "organization": org_key,
            "key": SETTING_KEY,
            "value": SETTING_VALUE,
        }
    ).encode("utf-8")

    return urllib.request.Request(
        API_URL,
        data=form_data,
        method="POST",
        headers={
            "Authorization": basic_auth_header(token),
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )


def build_v1_get_request(path: str, token: str, params: dict[str, str] | None = None) -> urllib.request.Request:
    url = f"{V1_BASE_URL}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": basic_auth_header(token), "User-Agent": USER_AGENT},
    )


def build_bearer_request(path: str, token: str, params: dict[str, str] | None = None) -> urllib.request.Request:
    url = f"{V2_BASE_URL}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
    )


def safe_error_body(response: object) -> str:
    try:
        body = response.read(ERROR_BODY_LIMIT + 1)  # type: ignore[attr-defined]
    except Exception:
        return ""

    if not body:
        return ""

    truncated = len(body) > ERROR_BODY_LIMIT
    body = body[:ERROR_BODY_LIMIT]
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    if truncated:
        text += "..."
    return text


def http_get_json(request: urllib.request.Request):
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", response.getcode())
            body = response.read()
    except urllib.error.HTTPError as exc:
        message = f"HTTP {exc.code} {exc.reason}"
        error_body = safe_error_body(exc)
        if error_body:
            message += f": {error_body}"
        raise ValueError(message) from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"network error: {getattr(exc, 'reason', exc)}") from exc
    except TimeoutError as exc:
        raise ValueError("network error: request timed out") from exc

    if not (200 <= status < 300):
        raise ValueError(f"unexpected HTTP {status}")

    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON response: {exc}") from exc


def resolve_enterprise_id(enterprise_key: str, token: str) -> str:
    request = build_bearer_request("enterprises/enterprises", token, {"enterpriseKey": enterprise_key})
    try:
        data = http_get_json(request)
    except ValueError as exc:
        raise ValueError(f"failed to resolve enterprise '{enterprise_key}': {exc}") from exc

    if not data:
        raise ValueError(
            f"enterprise '{enterprise_key}' not found, or token lacks the "
            "Enterprise Administrator role for it"
        )

    enterprise_id = data[0].get("id")
    if not enterprise_id:
        raise ValueError(f"enterprise '{enterprise_key}' response is missing an id")
    return enterprise_id


def list_enterprise_organization_uuids(enterprise_id: str, token: str) -> list[str]:
    request = build_bearer_request("enterprises/enterprise-organizations", token, {"enterpriseId": enterprise_id})
    data = http_get_json(request)
    if not isinstance(data, list):
        raise ValueError("unexpected response format from the enterprise-organizations endpoint")

    org_uuids: list[str] = []
    seen: set[str] = set()
    for relation in data:
        org_uuid = relation.get("organizationUuidV4") or relation.get("organizationId")
        if org_uuid and org_uuid not in seen:
            seen.add(org_uuid)
            org_uuids.append(org_uuid)
    return org_uuids


def resolve_organization_keys(org_uuids: list[str], token: str) -> list[str]:
    org_keys: list[str] = []
    seen: set[str] = set()

    for start in range(0, len(org_uuids), ORG_IDS_PER_REQUEST):
        chunk = org_uuids[start : start + ORG_IDS_PER_REQUEST]
        request = build_bearer_request("organizations/organizations", token, {"ids": ",".join(chunk)})
        data = http_get_json(request)
        if not isinstance(data, list):
            raise ValueError("unexpected response format from the organizations endpoint")

        for organization in data:
            org_key = organization.get("key")
            if org_key and org_key not in seen:
                seen.add(org_key)
                org_keys.append(org_key)

    return org_keys


def get_org_keys_from_enterprise(enterprise_key: str, token: str) -> list[str]:
    enterprise_id = resolve_enterprise_id(enterprise_key, token)
    org_uuids = list_enterprise_organization_uuids(enterprise_id, token)
    if not org_uuids:
        raise ValueError(f"enterprise '{enterprise_key}' has no attached organizations")
    return resolve_organization_keys(org_uuids, token)


def dump_org_keys_csv(org_keys: list[str], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        for org_key in org_keys:
            writer.writerow([org_key])


def fetch_org_admin_flag(org_key: str, token: str) -> bool:
    request = build_v1_get_request("organizations/search", token, {"organizations": org_key})
    data = http_get_json(request)
    organizations = data.get("organizations", [])
    if not organizations:
        raise ValueError(f"organization '{org_key}' not found or not visible to this token")
    return bool(organizations[0].get("actions", {}).get("admin", False))


def fetch_sca_setting(org_key: str, token: str) -> str | None:
    request = build_v1_get_request("settings/values", token, {"organization": org_key, "keys": SETTING_KEY})
    data = http_get_json(request)
    settings = data.get("settings", [])
    if not settings:
        return None
    return settings[0].get("value")


def print_status_report(org_keys: list[str], token: str) -> int:
    header = f"{'ORGANIZATION':<40} {'ADMIN':<7} {SETTING_KEY.upper()}"
    print(header)
    print("-" * len(header))

    had_error = False
    for org_key in org_keys:
        try:
            is_admin = fetch_org_admin_flag(org_key, token)
        except ValueError as exc:
            print(f"{org_key:<40} ERROR: {exc}")
            had_error = True
            continue

        try:
            sca_value = fetch_sca_setting(org_key, token)
        except ValueError as exc:
            print(f"{org_key:<40} {'yes' if is_admin else 'no':<7} ERROR: {exc}")
            had_error = True
            continue

        status = "not set (default)" if sca_value is None else sca_value
        print(f"{org_key:<40} {'yes' if is_admin else 'no':<7} {status}")

    return 1 if had_error else 0


def disable_sca(org_key: str, token: str) -> Result:
    request = build_request(org_key, token)

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", response.getcode())
            if 200 <= status < 300:
                return Result(org_key, True, f"HTTP {status}")
            return Result(org_key, False, f"unexpected HTTP {status}")
    except urllib.error.HTTPError as exc:
        message = f"HTTP {exc.code} {exc.reason}"
        body = safe_error_body(exc)
        if body:
            message += f": {body}"
        return Result(org_key, False, message)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return Result(org_key, False, f"network error: {reason}")
    except TimeoutError:
        return Result(org_key, False, "network error: request timed out")
    except OSError as exc:
        return Result(org_key, False, f"network error: {exc}")


def print_summary(results: list[Result], dry_run: bool) -> int:
    total = len(results)
    succeeded = sum(1 for result in results if result.ok)
    failed_results = [result for result in results if not result.ok]
    failed = len(failed_results)

    label = "DRY-RUN SUMMARY" if dry_run else "SUMMARY"
    print(f"{label}: total={total}, succeeded={succeeded}, failed={failed}")

    if failed_results:
        failed_keys = ", ".join(result.organization for result in failed_results)
        print(f"Failed organization keys: {failed_keys}")

    return 1 if failed else 0


def get_org_keys(args: argparse.Namespace, token: str) -> list[str]:
    if args.enterprise_key:
        return get_org_keys_from_enterprise(args.enterprise_key, token)
    if args.csv:
        return read_org_keys(args.csv, from_flag=True)
    return read_org_keys(get_csv_path())


def main() -> int:
    args = parse_args()
    token = require_token()

    try:
        org_keys = get_org_keys(args, token)
    except ValueError as exc:
        print(f"Error: {exc}; no changes were made.", file=sys.stderr)
        return 1

    if not org_keys:
        print("Error: no organization keys found; no changes were made.", file=sys.stderr)
        return 1

    if args.dump_csv:
        dump_org_keys_csv(org_keys, args.dump_csv)
        print(f"Wrote {len(org_keys)} organization key(s) to {args.dump_csv}")
        return 0

    if args.list_status:
        return print_status_report(org_keys, token)

    if not confirm(org_keys, args.dry_run):
        print("No changes were made.")
        return 0

    results: list[Result] = []
    for org_key in org_keys:
        if args.dry_run:
            print(
                f"DRY-RUN {org_key}: would POST {API_URL} "
                f"with organization={org_key}, key={SETTING_KEY}, value={SETTING_VALUE}"
            )
            results.append(Result(org_key, True, "dry-run"))
            continue

        result = disable_sca(org_key, token)
        results.append(result)
        status = "SUCCESS" if result.ok else "FAILURE"
        print(f"{status} {org_key}: {result.message}")

    return print_summary(results, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
