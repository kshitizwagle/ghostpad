"""Tests for package_spike.py. Run from repo root: python -m unittest discover -s client/tizen"""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import package_spike as ps


class PackageSpikeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "s.wgt"

    def tearDown(self):
        self.tmp.cleanup()

    def config_of(self, z: zipfile.ZipFile) -> dict:
        text = z.read(ps.CONFIG_JS).decode()
        return json.loads(text.split("=", 1)[1].strip().rstrip(";"))

    def test_real_spike_folder(self):
        names = ps.build(self.out, "192.168.1.100", 47800, "")
        self.assertEqual(names[0], "config.xml")
        for need in ("index.html", "main.js", "protocol.js", "style.css", "icon.png", ps.CONFIG_JS):
            self.assertIn(need, names)
        with zipfile.ZipFile(self.out) as z:
            self.assertEqual(z.namelist(), names)
            self.assertEqual(self.config_of(z), {"pcHost": "192.168.1.100", "pcPort": 47800, "token": "",
                                                 "autoConnect": True})
            self.assertEqual(z.read("main.js"), (ps.SPIKE / "main.js").read_bytes())

    def test_signing_material_and_dotfiles_skipped(self):
        root = Path(self.tmp.name) / "spike"
        (root / ".sign").mkdir(parents=True)
        for name in ("config.xml", "index.html", "author-signature.xml", "signature1.xml",
                     "dev.p12", "old.wgt", ".sign/x.p12", ".hidden"):
            (root / name).write_text("x")
        names = ps.build(self.out, "", 47800, 'a"b', root=root, auto_connect=False)
        self.assertEqual(names, ["config.xml", "index.html", ps.CONFIG_JS])
        with zipfile.ZipFile(self.out) as z:
            self.assertEqual(self.config_of(z)["token"], 'a"b')
            self.assertIs(self.config_of(z)["autoConnect"], False)

    def test_host_validation(self):
        for ok in ("", "192.168.1.100", "fe80::1", "gaming-pc", "pc.lan"):
            self.assertTrue(ps.valid_host(ok), ok)
        for bad in ("pc'; alert(1)//", "a b", "-x", "http://pc"):
            self.assertFalse(ps.valid_host(bad), bad)


if __name__ == "__main__":
    unittest.main()
