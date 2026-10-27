"""Installation-layout regressions using copied packages and isolated state."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "agent-efficiency"
SCRIPTS = PLUGIN / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_efficiency as ae


class InstallVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ae-install-verifier-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {
            "AGENT_EFFICIENCY_DATA": str(self.base / "state"),
            "AGENT_EFFICIENCY_DISABLE_LIVE_REFRESH": "1",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def _package(self, relative: str, version: str = "0.5.0+codex.test-1") -> Path:
        root = self.base / "packages" / relative
        shutil.copytree(PLUGIN, root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        manifest_path = root / ".codex-plugin" / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = version
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def _verify(self, root: Path) -> dict:
        def snapshot() -> dict[str, bytes]:
            return {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*") if path.is_file()
            }

        before = snapshot()
        result = ae.verify_install(root)
        self.assertEqual(snapshot(), before, "verification must not rewrite package files")
        return result

    def _assert_layout_rejected(self, root: Path) -> None:
        result = self._verify(root)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["checks"]["manifest_schema"]["ok"])
        self.assertTrue(any("plugin directory" in error for error in result["errors"]))

    def test_source_directory_passes(self) -> None:
        result = self._verify(self._package("source/agent-efficiency"))
        self.assertEqual(result["status"], "pass", result["errors"])

    def test_exact_installed_version_directory_passes(self) -> None:
        # The parent location can be a custom cache root; only the identity
        # components, not the user's home or marketplace directory, matter.
        for version in ("0.5.0", "0.5.0+codex.test-1"):
            with self.subTest(version=version):
                root = self._package(f"custom-cache/agent-efficiency/{version}", version)
                result = self._verify(root)
                self.assertEqual(result["status"], "pass", result["errors"])
                self.assertTrue(result["checks"]["manifest_schema"]["ok"])
                self.assertTrue(result["checks"]["version"]["ok"])

    def test_wrong_source_directory_is_rejected(self) -> None:
        self._assert_layout_rejected(self._package("source/wrong-plugin"))

    def test_wrong_installed_plugin_parent_is_rejected(self) -> None:
        self._assert_layout_rejected(self._package("wrong-plugin/0.5.0+codex.test-1"))

    def test_wrong_installed_cachebuster_is_rejected(self) -> None:
        self._assert_layout_rejected(self._package("agent-efficiency/0.5.0+codex.test-2"))

    def test_base_version_alone_cannot_match_a_cachebusted_manifest(self) -> None:
        self._assert_layout_rejected(self._package("agent-efficiency/0.5.0"))

    def test_arbitrary_child_of_plugin_directory_is_rejected(self) -> None:
        self._assert_layout_rejected(self._package("agent-efficiency/arbitrary"))

    def test_matching_layout_still_rejects_core_version_mismatch(self) -> None:
        root = self._package("agent-efficiency/99.0.0+codex.test-1", "99.0.0+codex.test-1")
        result = self._verify(root)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(result["checks"]["manifest_schema"]["ok"])
        self.assertFalse(result["checks"]["version"]["ok"])
        self.assertIn("core and manifest base versions differ", result["errors"])

    def test_malformed_identity_fields_are_rejected_without_throwing(self) -> None:
        root = self._package("agent-efficiency/0.5.0+codex.test-1")
        manifest_path = root / ".codex-plugin" / "plugin.json"
        valid = json.loads(manifest_path.read_text(encoding="utf-8"))
        for field in ("name", "version"):
            for value in (None, "", "   ", 123, [], {}):
                with self.subTest(field=field, value=value):
                    manifest = dict(valid)
                    manifest[field] = value
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    result = self._verify(root)
                    self.assertEqual(result["status"], "fail")
                    self.assertIn(
                        f"manifest field {field} must be a non-empty string", result["errors"]
                    )
            with self.subTest(field=field, missing=True):
                manifest = dict(valid)
                del manifest[field]
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                self.assertEqual(self._verify(root)["status"], "fail")

    def test_installed_layout_still_requires_companion_files(self) -> None:
        root = self._package("agent-efficiency/0.5.0+codex.test-1")
        (root / ".mcp.json").unlink()  # Only this test-created package copy.
        result = self._verify(root)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["checks"]["mcp_config"]["ok"])


if __name__ == "__main__":
    unittest.main()
