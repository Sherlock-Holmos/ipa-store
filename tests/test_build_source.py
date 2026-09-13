import argparse
import plistlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_source  # noqa: E402


class BuildSourceTests(unittest.TestCase):
    def test_builds_app_metadata_and_extracts_icon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ipa_dir = root / "ipas"
            icon_dir = root / "icons"
            ipa_dir.mkdir()
            ipa_path = ipa_dir / "demo.ipa"
            plist = {
                "CFBundleIdentifier": "com.example.demo",
                "CFBundleDisplayName": "Demo App",
                "CFBundleShortVersionString": "1.2.3",
                "MinimumOSVersion": "15.0",
                "CFBundleIconFiles": ["Icon.png"],
            }
            with zipfile.ZipFile(ipa_path, "w") as archive:
                archive.writestr("Payload/Demo.app/Info.plist", plistlib.dumps(plist))
                archive.writestr("Payload/Demo.app/Icon.png", b"not-a-real-png")

            config_path = root / "source.config.json"
            config_path.write_text(
                '{"name":"Test Source","identifier":"com.example.source",'
                '"publicBaseURL":"https://example.com"}',
                encoding="utf-8",
            )
            args = argparse.Namespace(
                config=config_path,
                overrides=root / "missing-overrides.json",
                output=root / "source.json",
                icon_dir=icon_dir,
                ipa_dir=ipa_dir,
                github_repo=None,
                public_base_url="https://example.com",
                include_prereleases=False,
            )

            source = build_source.build(args)

            self.assertEqual(len(source["apps"]), 1)
            app = source["apps"][0]
            self.assertEqual(app["name"], "Demo App")
            self.assertEqual(app["bundleIdentifier"], "com.example.demo")
            self.assertEqual(app["version"], "1.2.3")
            self.assertEqual(app["versions"][0]["minOSVersion"], "15.0")
            self.assertEqual(app["iconURL"], "https://example.com/icons/com.example.demo.png")
            self.assertTrue((icon_dir / "com.example.demo.png").exists())

    def test_empty_source_is_valid(self) -> None:
        build_source.validate_source(
            {"name": "Test Source", "identifier": "com.example.source", "apps": []}
        )


if __name__ == "__main__":
    unittest.main()
