#!/usr/bin/env python3
"""Offline tests for the read-only code paths of sonar_disable_sca.py.

No network access and no real SONAR_TOKEN are used: urllib.request.urlopen
is faked for every test, and any attempted POST (i.e. any real settings
change) fails the test immediately. This lets the read-only surface
(--list-status, --dump-csv, --dry-run, and --csv as an input source for
either of them) be exercised safely, repeatedly, offline.

Run: python3 test_sonar_disable_sca.py
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
import urllib.error
import urllib.parse

import sonar_disable_sca as sca

FAKE_TOKEN = "fake-token-for-tests"


class _FakeResponse:
    status = 200

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def http_error(url: str, code: int, message: str) -> urllib.error.HTTPError:
    body = json.dumps({"errors": [{"msg": message}]}).encode("utf-8")
    return urllib.error.HTTPError(url, code, message, None, io.BytesIO(body))


class NoWriteGuard(unittest.TestCase):
    """Base class: any POST request during a test is treated as a bug."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(sca.urllib.request, "urlopen", side_effect=self._urlopen)
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)

    def _urlopen(self, request, timeout=None):
        if request.get_method() == "POST":
            raise AssertionError(f"read-only path attempted a POST to {request.full_url}")
        return self.fake_get(request)

    def fake_get(self, request):  # pragma: no cover - overridden per test class
        raise AssertionError(f"unexpected GET {request.full_url}")

    @staticmethod
    def query_params(request):
        return urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)

    @staticmethod
    def path(request):
        return urllib.parse.urlparse(request.full_url).path


def run_main(argv):
    """Run sca.main() with argv/env/getpass/confirm faked.

    Returns (exit_code, combined stdout+stderr) — combined because the script
    intentionally prints user-facing errors to stderr (see main()) while
    everything else goes to stdout, and tests care about the message, not
    which stream it landed on.
    """
    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(sys, "argv", ["sonar_disable_sca.py", *argv]), \
        mock.patch.dict(os.environ, {"SONAR_TOKEN": FAKE_TOKEN}), \
        mock.patch("getpass.getpass", return_value=""), \
        mock.patch.object(sca, "read_single_key", return_value="y"), \
        contextlib.redirect_stdout(stdout), \
        contextlib.redirect_stderr(stderr):
        exit_code = sca.main()
    return exit_code, stdout.getvalue() + stderr.getvalue()


class ArgParsingTests(unittest.TestCase):
    def parse(self, argv):
        with mock.patch.object(sys, "argv", ["sonar_disable_sca.py", *argv]), \
            contextlib.redirect_stderr(io.StringIO()):
            return sca.parse_args()

    def test_dump_csv_requires_enterprise_key(self):
        with self.assertRaises(SystemExit):
            self.parse(["--dump-csv", "out.csv"])

    def test_csv_and_enterprise_key_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self.parse(["--csv", "orgs.csv", "--enterprise-key", "acme"])

    def test_dump_csv_and_list_status_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self.parse(["--enterprise-key", "acme", "--dump-csv", "out.csv", "--list-status"])

    def test_csv_and_list_status_combine(self):
        args = self.parse(["--csv", "orgs.csv", "--list-status"])
        self.assertEqual(args.csv, "orgs.csv")
        self.assertTrue(args.list_status)

    def test_enterprise_key_and_list_status_combine(self):
        args = self.parse(["--enterprise-key", "acme", "--list-status"])
        self.assertEqual(args.enterprise_key, "acme")
        self.assertTrue(args.list_status)


class ListStatusFromCsvTests(NoWriteGuard):
    """--list-status combined with --csv as the org-key source."""

    ORG_ADMIN_DISABLED = "org-admin-disabled"
    ORG_MEMBER_DEFAULT = "org-member-default"
    ORG_FORBIDDEN = "org-forbidden"

    def setUp(self):
        super().setUp()
        fd, self.csv_path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(self.csv_path, "w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            for org in (self.ORG_ADMIN_DISABLED, self.ORG_MEMBER_DEFAULT, self.ORG_FORBIDDEN):
                writer.writerow([org])
        self.addCleanup(os.remove, self.csv_path)

    def fake_get(self, request):
        params = self.query_params(request)
        if self.path(request).endswith("/organizations/search"):
            org = params["organizations"][0]
            if org == self.ORG_FORBIDDEN:
                raise http_error(request.full_url, 403, f"You're not member of organization '{org}'")
            is_admin = org == self.ORG_ADMIN_DISABLED
            return _FakeResponse({"organizations": [{"key": org, "actions": {"admin": is_admin}}]})
        if self.path(request).endswith("/settings/values"):
            org = params["organization"][0]
            if org == self.ORG_ADMIN_DISABLED:
                return _FakeResponse({"settings": [{"key": sca.SETTING_KEY, "value": "false"}]})
            return _FakeResponse({"settings": []})
        raise AssertionError(f"unexpected GET {request.full_url}")

    def test_mixed_permissions_and_status_reported_without_writes(self):
        exit_code, output = run_main(["--csv", self.csv_path, "--list-status"])

        self.assertEqual(exit_code, 1)  # non-zero: at least one org errored
        self.assertIn(f"{self.ORG_ADMIN_DISABLED:<40} yes     false", output)
        self.assertIn(f"{self.ORG_MEMBER_DEFAULT:<40} no      not set (default)", output)
        self.assertIn(f"{self.ORG_FORBIDDEN:<40} ERROR: HTTP 403", output)
        self.assertIn("not member of organization", output)


class DumpCsvFromEnterpriseTests(NoWriteGuard):
    """--dump-csv combined with --enterprise-key: full V2 inventory chain, no writes."""

    ENTERPRISE_KEY = "acme"
    ENTERPRISE_ID = "ent-1234"

    def fake_get(self, request):
        path = self.path(request)
        params = self.query_params(request)

        if path.endswith("/enterprises/enterprises"):
            if params.get("enterpriseKey", [None])[0] != self.ENTERPRISE_KEY:
                return _FakeResponse([])
            return _FakeResponse([{"id": self.ENTERPRISE_ID, "key": self.ENTERPRISE_KEY, "name": "Acme"}])

        if path.endswith("/enterprises/enterprise-organizations"):
            self.assertEqual(params["enterpriseId"][0], self.ENTERPRISE_ID)
            return _FakeResponse(
                [
                    {"organizationUuidV4": "uuid-1", "organizationId": "uuid-1"},
                    {"organizationUuidV4": "uuid-2", "organizationId": "uuid-2"},
                ]
            )

        if path.endswith("/organizations/organizations"):
            ids = params["ids"][0].split(",")
            catalog = {"uuid-1": "org-one", "uuid-2": "org-two"}
            return _FakeResponse([{"key": catalog[i], "uuidV4": i} for i in ids])

        raise AssertionError(f"unexpected GET {request.full_url}")

    def test_inventory_written_to_csv_with_no_writes(self):
        fd, out_path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        self.addCleanup(os.remove, out_path)

        exit_code, output = run_main(["--enterprise-key", self.ENTERPRISE_KEY, "--dump-csv", out_path])

        self.assertEqual(exit_code, 0)
        self.assertIn(f"Wrote 2 organization key(s) to {out_path}", output)
        self.assertEqual(sca.read_org_keys(out_path), ["org-one", "org-two"])

    def test_unknown_enterprise_key_fails_cleanly_with_no_writes(self):
        exit_code, output = run_main(["--enterprise-key", "not-acme", "--dump-csv", "unused.csv"])

        self.assertEqual(exit_code, 1)
        self.assertIn("Enterprise Administrator role", output)
        self.assertFalse(os.path.exists("unused.csv"))


class DryRunFromCsvTests(unittest.TestCase):
    """--dry-run: confirmation still happens, but zero HTTP calls of any kind."""

    def test_dry_run_makes_no_network_calls(self):
        fd, csv_path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        self.addCleanup(os.remove, csv_path)
        with open(csv_path, "w", newline="", encoding="utf-8") as csv_file:
            csv.writer(csv_file).writerow(["org-one"])

        with mock.patch.object(sca.urllib.request, "urlopen") as urlopen_mock:
            exit_code, output = run_main(["--csv", csv_path, "--dry-run"])

        urlopen_mock.assert_not_called()
        self.assertEqual(exit_code, 0)
        self.assertIn("dry-run, no changes will be made", output)
        self.assertIn("DRY-RUN org-one", output)
        self.assertIn("DRY-RUN SUMMARY: total=1, succeeded=1, failed=0", output)


if __name__ == "__main__":
    unittest.main()
