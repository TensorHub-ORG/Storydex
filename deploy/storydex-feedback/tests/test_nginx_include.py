from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).parents[1] / "install_nginx_include.py"
SPEC = importlib.util.spec_from_file_location("storydex_feedback_nginx", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class NginxIncludeTests(unittest.TestCase):
    def test_inserts_once_in_matching_server(self) -> None:
        source = """server { server_name other.example; }\nserver {\n    listen 443 ssl;\n    server_name updates.septemc.com;\n}\n"""
        expected = "include /srv/storydex/nginx-location.conf;"
        updated = module.install_include(source, "/srv/storydex/nginx-location.conf")
        self.assertIn(expected, updated)
        self.assertEqual(module.install_include(updated, "/srv/storydex/nginx-location.conf"), updated)
        self.assertGreater(updated.index(expected), updated.index("updates.septemc.com"))

    def test_inserts_website_stats_include(self) -> None:
        source = """server { server_name other.example; }\nserver {\n    server_name storydex.septemc.com;\n}\n"""
        expected = "include /srv/storydex/website-nginx-location.conf;"
        updated = module.install_include(
            source,
            "/srv/storydex/website-nginx-location.conf",
            "storydex.septemc.com",
        )
        self.assertIn(expected, updated)
        self.assertEqual(
            module.install_include(
                updated,
                "/srv/storydex/website-nginx-location.conf",
                "storydex.septemc.com",
            ),
            updated,
        )

    def test_installer_materializes_detected_service_identity(self) -> None:
        package_root = Path(__file__).parents[1]
        installer = (package_root / "install.sh").read_text(encoding="utf-8")
        service = (package_root / "storydex-feedback.service").read_text(encoding="utf-8")

        self.assertIn("User=__SERVICE_USER__", service)
        self.assertIn("Group=__SERVICE_GROUP__", service)
        self.assertIn("ExecStart=__PYTHON__", service)
        self.assertIn("python_bin=/usr/bin/python3", installer)
        self.assertIn("sys.version_info >= (3, 6)", installer)
        self.assertIn('s|__PYTHON__|$python_bin|g', installer)
        self.assertIn('s|__SERVICE_USER__|$service_user|g', installer)
        self.assertIn('s|__SERVICE_GROUP__|$service_group|g', installer)
        self.assertIn("nginx_bin=/www/server/nginx/sbin/nginx", installer)
        self.assertIn("nginx_args=(-p /www/server/nginx/ -c conf/nginx.conf)", installer)
        self.assertIn("/etc/init.d/nginx reload", installer)
        self.assertIn("storydex.septemc.com", installer)
        self.assertIn("website-nginx-location.conf", installer)
        self.assertGreaterEqual(installer.count("nginx_test"), 3)
        self.assertGreaterEqual(installer.count("nginx_reload"), 3)


if __name__ == "__main__":
    unittest.main()
