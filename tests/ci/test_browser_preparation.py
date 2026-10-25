"""Exercise package-plan and archive failures without installing host packages."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import shutil
import subprocess
import tempfile
import threading
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
        mirror = record.replace("https://example.test", "mirror+file:/etc/apt/apt-mirrors.txt")
        self.assertEqual(HELPER.archive_plan(mirror)[0].size, 3)
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
        policy: str = "APT::Architecture",
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
                    return subprocess.CompletedProcess(args, 0, policy + "\n", "")
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
                if args[0] == HELPER.APT_HELPER:
                    if download_failure:
                        raise subprocess.CalledProcessError(4, args)
                    self.assertEqual(args[1:3], ["download-file", "https://example.test/font.deb"])
                    self.assertEqual(args[4], "SHA256:" + digest)
                    self.assertEqual(args[5], f"Checksum-FileSize:{len(payload)}")
                    self.assertEqual(len(args), 6)  # No transport configuration overrides.
                    Path(args[3]).write_bytes(b"X" * len(payload) if corruption else payload)
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
        self.assertEqual(sum(args[0] == HELPER.APT_HELPER for args in calls), 1)

    def test_custom_transport_policy_keeps_original_installer(self) -> None:
        for key in (
            "Acquire::http::Proxy",
            "Acquire::https::Dl-Limit",
            "Acquire::ForceIPv4",
            "Acquire::http::Timeout::unknown",
            "Acquire::Queue-Mode",
            "Acquire::ForceHash",
            "Binary::apt-get::Acquire::http::Timeout",
            "acquire::HTTP::Proxy",
            "Acquire::http::User-Agent-Non-Interactive::private-host",
            "Acquire::https::User-Agent-Non-Interactive",
            "Acquire::http::User-Agent",
        ):
            with self.subTest(key=key), patch.object(HELPER.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, key + "\n", "")
                HELPER.prepare(Path("/unused"), Path("/unused"))
                self.assertEqual(run.call_args_list[-1].args[0], HELPER.INSTALL)
                self.assertEqual(run.call_count, 2)

    def test_observed_runner_controls_use_native_apt_without_transport_overrides(self) -> None:
        policy = "\n".join(
            f"Acquire::{protocol}::{key}"
            for protocol in ("http", "https")
            for key in ("Timeout", "Pipeline-Depth", "No-Cache")
        )
        calls = self.exercise(policy=policy)
        self.assertTrue(any(args[0] == HELPER.APT_HELPER for args in calls))

    def test_confirmed_direct_noninteractive_agent_control_stays_in_native_apt(self) -> None:
        calls = self.exercise(policy="Acquire::http::User-Agent-Non-Interactive")
        native = next(args for args in calls if args[0] == HELPER.APT_HELPER)
        self.assertEqual(native[1], "download-file")
        self.assertEqual(len(native), 6)
        self.assertNotIn("-o", native)  # Do not replace the configured flag/header/context.

    def test_prefetch_has_at_most_four_concurrent_archive_downloads(self) -> None:
        lock = threading.Lock()
        barrier = threading.Barrier(4)
        active = peak = completed = 0

        def download(archive: object, temporary: Path) -> None:
            nonlocal active, peak, completed
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=3)
            with lock:
                active -= 1
                completed += 1

        with patch.object(HELPER, "download", side_effect=download):
            HELPER.prefetch([object() for _ in range(8)], Path("/unused"))
        self.assertEqual((peak, completed), (4, 8))

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

    def test_mixed_known_and_nested_keys_remain_visible_and_block_prefetch(self) -> None:
        with (
            patch.object(HELPER.subprocess, "run") as run,
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            run.return_value = subprocess.CompletedProcess(
                [], 0, "Acquire::http::Timeout\nAcquire::http::Proxy::private-host\n", ""
            )
            HELPER.prepare(Path("/unused"), Path("/unused"))
            self.assertIn("http:Timeout", out.getvalue())
            self.assertIn("http:other-or-nested-key", out.getvalue())
            self.assertIn("http:qualified-Proxy", out.getvalue())
            self.assertIn("unsupported-transport-key", out.getvalue())
            self.assertNotIn("private-host", out.getvalue())
            self.assertEqual(run.call_args_list[-1].args[0], HELPER.INSTALL)

    def test_host_qualified_control_names_do_not_expose_the_private_host(self) -> None:
        with (
            patch.object(HELPER.subprocess, "run") as run,
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            run.return_value = subprocess.CompletedProcess(
                [], 0, "Acquire::http::Timeout\nAcquire::http::private-host::Timeout\n", ""
            )
            HELPER.prepare(Path("/unused"), Path("/unused"))
            self.assertIn("http:qualified-Timeout", out.getvalue())
            self.assertIn("unsupported-transport-key", out.getvalue())
            self.assertNotIn("private-host", out.getvalue())
            self.assertEqual(run.call_count, 2)

    def test_safe_direct_identifier_is_visible_without_changing_eligibility(self) -> None:
        with (
            patch.object(HELPER.subprocess, "run") as run,
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            run.return_value = subprocess.CompletedProcess(
                [], 0, "Acquire::http::CaseProbe_1\n", ""
            )
            HELPER.prepare(Path("/unused"), Path("/unused"))
            self.assertIn('"http": ["CaseProbe_1"]', out.getvalue())
            self.assertIn("unsupported-transport-key", out.getvalue())
            self.assertEqual(run.call_count, 2)

    def test_non_identifier_direct_fields_are_counted_without_revealing_text(self) -> None:
        for suffix in (
            "private.host",
            "private/host",
            "private@host",
            "private host",
            "private\\host",
            "http://private-host",
            "X" * 65,
            "非ASCII",
        ):
            with (
                self.subTest(kind=suffix),
                patch.object(HELPER.subprocess, "run") as run,
                patch("sys.stdout", new_callable=io.StringIO) as out,
            ):
                run.return_value = subprocess.CompletedProcess(
                    [], 0, f"Acquire::http::{suffix}\n", ""
                )
                HELPER.prepare(Path("/unused"), Path("/unused"))
                self.assertNotIn(suffix, out.getvalue())
                self.assertIn('"hidden_unsupported_key_count": 1', out.getvalue())
                self.assertEqual(run.call_count, 2)
        with (
            patch.object(HELPER.subprocess, "run") as run,
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            run.return_value = subprocess.CompletedProcess([], 0, "Acquire::http::\n", "")
            HELPER.prepare(Path("/unused"), Path("/unused"))
            self.assertIn('"empty_direct_key_count": 1', out.getvalue())
            self.assertEqual(run.call_count, 2)

    def test_other_guard_reasons_are_classified_without_raw_keys_or_configuration(self) -> None:
        for key, reason in (
            ("Acquire::Queue-Mode", "queue-control"),
            ("Acquire::ForceHash", "hash-control"),
            ("Binary::apt-get::Acquire::http::Timeout", "apt-get-binary-control"),
            ("Binary::apt-helper::Acquire::http::Timeout", "apt-helper-binary-control"),
        ):
            with (
                self.subTest(reason=reason),
                patch.object(HELPER.subprocess, "run") as run,
                patch("sys.stdout", new_callable=io.StringIO) as out,
            ):
                run.return_value = subprocess.CompletedProcess([], 0, key + "\n", "")
                HELPER.prepare(Path("/unused"), Path("/unused"))
                self.assertIn(reason, out.getvalue())
                self.assertEqual(run.call_count, 2)
        with (
            patch.dict("os.environ", {"APT_CONFIG": "/private/config"}),
            patch.object(HELPER.subprocess, "run") as run,
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            HELPER.prepare(Path("/unused"), Path("/unused"))
            self.assertIn("alternate-config", out.getvalue())
            self.assertNotIn("/private/config", out.getvalue())
            self.assertEqual(run.call_count, 2)

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
