#!/usr/bin/env python3
r"""
Disable SonarQube Cloud SCA by default for organizations listed in orgs.csv.

This script is for SonarQube Cloud only, not SonarQube Server.

Input:
    orgs.csv in the current working directory, one organization key per row, no header.

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
"""

from __future__ import annotations

import argparse
import base64
import csv
import getpass
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


API_URL = "https://sonarcloud.io/api/settings/set"
DEFAULT_CSV_PATH = "orgs.csv"
SETTING_KEY = "sonar.sca.enabled"
SETTING_VALUE = "false"
TIMEOUT_SECONDS = 30
ERROR_BODY_LIMIT = 800
USER_AGENT = "disable-sca-org-default/1.0 (Python urllib; standard-library)"


@dataclass(frozen=True)
class Result:
    organization: str
    ok: bool
    message: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Disable SonarQube Cloud SCA by default for orgs in orgs.csv."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse, preview, and confirm, but make no API calls.",
    )
    return parser.parse_args()


def read_org_keys(path: str) -> list[str]:
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


def confirm(org_keys: list[str]) -> bool:
    print(f"Total unique organizations: {len(org_keys)}")
    print("Organization keys: " + ", ".join(org_keys))
    print("Continue and disable SCA by default for these organizations? [y/N]: ", end="", flush=True)
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


def build_request(org_key: str, token: str) -> urllib.request.Request:
    form_data = urllib.parse.urlencode(
        {
            "organization": org_key,
            "key": SETTING_KEY,
            "value": SETTING_VALUE,
        }
    ).encode("utf-8")

    credentials = f"{token}:".encode("utf-8")
    encoded_credentials = base64.b64encode(credentials).decode("ascii")

    return urllib.request.Request(
        API_URL,
        data=form_data,
        method="POST",
        headers={
            "Authorization": f"Basic {encoded_credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
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


def main() -> int:
    args = parse_args()
    token = require_token()
    csv_path = get_csv_path()

    try:
        org_keys = read_org_keys(csv_path)
    except ValueError as exc:
        print(f"Error: {exc}; no changes were made.", file=sys.stderr)
        return 1

    if not org_keys:
        print("Error: orgs.csv contains no organization keys; no changes were made.", file=sys.stderr)
        return 1

    if not confirm(org_keys):
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
