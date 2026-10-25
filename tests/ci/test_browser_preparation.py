"""Exercise package-plan and archive failures without installing host packages."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "browser_preparation", ROOT / "scripts/dev-env/prepare-ci-browser.py"
)
assert SPEC and SPEC.loader
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class PackagePlanTests(unittest.TestCase):
    def test_locked_cli_inventory_includes_upgrades_and_transitive_packages(self) -> None:
        result = subprocess.CompletedProcess(
            [], 1, "Missing system dependencies (2):\n  fonts-unifont\n  libglib2.0-0t64\n", ""
        )
        self.assertEqual(HELPER.missing_packages(result), ["fonts-unifont", "libglib2.0-0t64"])
        self.assertEqual(
            HELPER.missing_packages(
                subprocess.CompletedProcess([], 0, "All system dependencies are installed.\n", "")
            ),
            [],
        )

    def test_unknown_or_injected_package_output_never_becomes_a_command(self) -> None:
        for code, output in (
            (100, "Missing system dependencies (1):\n  fonts-unifont"),
            (0, ""),
            (1, "Missing system dependencies (2):\n  fonts-unifont"),
            (1, "Missing system dependencies (2):\n  liba\n  liba"),
            (1, "Missing system dependencies (1):\n  --allow-unauthenticated"),
            (1, "Missing system dependencies (1):\n  liba;touch /tmp/never"),
        ):
            with self.subTest(output=output), self.assertRaises(ValueError):
                HELPER.missing_packages(subprocess.CompletedProcess([], code, output, ""))

    def test_archive_plan_requires_safe_names_size_and_checksums(self) -> None:
        digest = "a" * 64
        record = f"'https://example.test/pool/liba.deb' liba_1_all.deb 3 SHA256:{digest}"
        self.assertEqual(HELPER.archive_plan("Reading package lists...\n" + record)[0].size, 3)
        for malformed in (
            record + "\n" + record,
            record.replace("liba_1_all.deb", "../liba.deb"),
            record.replace("https://example.test", "https://user:secret@example.test"),
            record.replace("https://example.test", "file:///tmp"),
            record.replace("pool/liba.deb", "pool/liba.deb?token=synthetic"),
            record.replace(" 3 ", " 0 "),
            record.replace("SHA256:" + digest, "SHA256:bad"),
            record.replace("SHA256:" + digest, "MD5Sum:" + "a" * 32),
            record.replace(" 3 SHA256:" + digest, " 3"),
            record.replace("pool/liba", "pool/\nout=bad"),
        ):
            with self.subTest(record=malformed), self.assertRaises(ValueError):
                HELPER.archive_plan(malformed)


class PreparationTests(unittest.TestCase):
    def exercise(
        self,
        *,
        corruption: bool = False,
        download_failure: bool = False,
        cached: bytes | None = None,
    ) -> list[list[str]]:
        calls: list[list[str]] = []
        payload = b"synthetic archive only"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            if cached is not None:
                (cache / "fonts-unifont_1_all.deb").write_bytes(cached)

            def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(args)
                if args[0] == "apt-config":
                    self.assertEqual(args, ["apt-config", "dump", "--format", "%f%n"])
                    return subprocess.CompletedProcess(args, 0, "APT::Architecture\n", "")
                if "--dry-run" in args:
                    return subprocess.CompletedProcess(
                        args, 1, "Missing system dependencies (1):\n  fonts-unifont\n", ""
                    )
                if "--print-uris" in args:
                    self.assertIn("Acquire::ForceHash=sha256", args)
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        f"'https://example.test/font.deb' fonts-unifont_1_all.deb "
                        f"{len(payload)} SHA256:{digest}\n",
                        "",
                    )
                if args[0] == "aria2c":
                    if download_failure:
                        raise subprocess.CalledProcessError(4, args)
                    temporary = Path(args[args.index("--dir") + 1])
                    self.assertIn("--max-concurrent-downloads=4", args)
                    self.assertIn("--max-connection-per-server=1", args)
                    self.assertIn("--no-conf", args)
                    self.assertNotIn("--check-certificate=false", args)
                    (temporary / "fonts-unifont_1_all.deb").write_bytes(
                        b"X" * len(payload) if corruption else payload
                    )
                elif args[:2] == ["sudo", "install"]:
                    self.assertEqual(args[2:4], ["--mode=0644", "--"])
                    shutil.copyfile(args[4], args[5])
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch.object(HELPER.subprocess, "run", side_effect=run):
                if corruption or download_failure or (cached is not None and cached != payload):
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        HELPER.prepare(cache, root)
                    self.assertNotIn(HELPER.INSTALL, calls)
                    if cached is None:
                        self.assertEqual(list(cache.iterdir()), [])
                    else:
                        self.assertEqual((cache / "fonts-unifont_1_all.deb").read_bytes(), cached)
                else:
                    HELPER.prepare(cache, root)
                    self.assertEqual((cache / "fonts-unifont_1_all.deb").read_bytes(), payload)
                    self.assertEqual(calls[-1], HELPER.INSTALL)
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["cache"])
        return calls

    def test_prefetch_verifies_bytes_before_official_install_and_cleans_temporary_files(
        self,
    ) -> None:
        self.exercise()

    def test_corrupt_archive_blocks_installer_and_cache_write(self) -> None:
        self.exercise(corruption=True)

    def test_download_error_blocks_installer_without_a_second_attempt(self) -> None:
        calls = self.exercise(download_failure=True)
        self.assertEqual(sum(args[0] == "aria2c" for args in calls), 1)

    def test_custom_transport_policy_keeps_original_installer(self) -> None:
        for key in ("Acquire::http::Proxy", "Acquire::https::Dl-Limit", "Acquire::ForceIPv4"):
            with self.subTest(key=key), patch.object(HELPER.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, key + "\n", "")
                HELPER.prepare(Path("/unused"), Path("/unused"))
                self.assertEqual(run.call_args_list[-1].args[0], HELPER.INSTALL)
                self.assertEqual(run.call_count, 2)

    def test_matching_cache_is_preserved_and_mismatching_cache_is_not_overwritten(self) -> None:
        calls = self.exercise(cached=b"synthetic archive only")
        self.assertFalse(any(args[:2] == ["sudo", "install"] for args in calls))
        self.exercise(cached=b"X" * len(b"synthetic archive only"))

    def test_policy_diagnostics_only_emit_fixed_categories(self) -> None:
        with (
            patch.object(HELPER.subprocess, "run") as run,
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            run.return_value = subprocess.CompletedProcess(
                [],
                0,
                "Acquire::http::Proxy\nAcquire::https::Proxy::private-host\n",
                "permission denied at /private/path user:secret",
            )
            HELPER.prepare(Path("/unused"), Path("/unused"))
            output = out.getvalue()
            self.assertIn("http:Proxy", output)
            self.assertIn("https:other-or-nested-key", output)
            self.assertIn("permission-denied", output)
            for private in ("private-host", "/private/path", "secret", "user:"):
                self.assertNotIn(private, output)
            self.assertEqual(run.call_args_list[-1].args[0], HELPER.INSTALL)

    def test_unreadable_root_policy_does_not_enable_an_alternate_transport(self) -> None:
        with patch.object(HELPER.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "unreadable configuration")
            HELPER.prepare(Path("/unused"), Path("/unused"))
            self.assertEqual(run.call_args_list[-1].args[0], HELPER.INSTALL)
            self.assertEqual(run.call_count, 2)

    def test_size_digest_and_symlinks_are_independent_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "archive.deb"
            target.write_bytes(b"abc")
            good = HELPER.Archive(
                "https://example.test/a.deb",
                target.name,
                3,
                "sha256",
                hashlib.sha256(b"abc").hexdigest(),
            )
            self.assertTrue(HELPER.verified(target, good))
            self.assertFalse(HELPER.verified(target, good._replace(size=4)))
            self.assertFalse(HELPER.verified(target, good._replace(digest="0" * 64)))
            link = root / "link.deb"
            link.symlink_to(target)
            self.assertFalse(HELPER.verified(link, good))


if __name__ == "__main__":
    unittest.main()
