import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from jetbrains_proxy import _walk_for_ides, inspect, install_patch, restore


SOURCE_JAR = Path(
    r"C:\Users\pc193i\AppData\Local\Programs\Android Studio\lib\intellij.platform.ide.jar.bak"
)


@unittest.skipUnless(SOURCE_JAR.is_file(), "Original Android Studio JAR is not available")
class InstallationLifecycleTest(unittest.TestCase):
    def test_patch_restore_and_repatch(self):
        original_hash = hashlib.sha256(SOURCE_JAR.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "MockStudio"
            lib = root / "lib"
            lib.mkdir(parents=True)
            jar = lib / "intellij.platform.ide.jar"
            jar.write_bytes(SOURCE_JAR.read_bytes())
            (root / "product-info.json").write_text(json.dumps({
                "name": "Mock Studio", "version": "1.0", "productCode": "MOCK",
                "launch": [],
            }), encoding="utf-8")

            discovered = _walk_for_ides(root)
            self.assertEqual(len(discovered), 1)
            installation = discovered[0]
            self.assertEqual(inspect(installation).state, "original")

            with mock.patch("patch_ide_proxy._verify_with_bundled_jvm"):
                self.assertEqual(install_patch(installation), "patched")
                self.assertEqual(inspect(installation).state, "patched")
                with zipfile.ZipFile(jar) as archive:
                    self.assertEqual(archive.comment, b"", "Stale JetBrains IKV cache must be removed")
                self.assertEqual(install_patch(installation), "already patched")
                self.assertEqual(
                    hashlib.sha256(installation.backup.read_bytes()).hexdigest(), original_hash
                )

                self.assertEqual(restore(installation), "restored")
                self.assertEqual(inspect(installation).state, "original")
                self.assertEqual(hashlib.sha256(jar.read_bytes()).hexdigest(), original_hash)

                self.assertEqual(install_patch(installation), "patched")
                self.assertEqual(inspect(installation).state, "patched")
                self.assertEqual(
                    hashlib.sha256(installation.backup.read_bytes()).hexdigest(), original_hash
                )


if __name__ == "__main__":
    unittest.main()
