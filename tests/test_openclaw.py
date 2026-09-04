import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nixloom.config import Config, RuntimePaths
from nixloom.openclaw import (
    IMAGE_SETTINGS,
    TAVILY_SETTINGS,
    YUANBAO_SECRET_SETTINGS,
    YUANBAO_SETTINGS,
    _set_batch,
    _sync_control_ui,
    _unset_paths,
    managed_settings,
    reconcile_settings,
    run,
)

ROOT = Path(__file__).resolve().parents[1]


class OpenClawTests(unittest.TestCase):
    def setUp(self) -> None:
        paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        self.config = Config.load(paths)

    def test_managed_provider_supports_reasoning_vision_and_images(self) -> None:
        settings = {
            item["path"]: item["value"] for item in managed_settings(self.config)
        }
        model = settings["models.providers.nixloom"]["models"][0]
        self.assertTrue(model["reasoning"])
        self.assertEqual(model["input"], ["text", "image"])
        self.assertIn("agents.defaults.imageGenerationModel", settings)
        self.assertEqual(settings["gateway.bind"], "loopback")
        self.assertEqual(settings["gateway.tailscale.mode"], "serve")
        self.assertEqual(settings["gateway.trustedProxies"], ["127.0.0.1"])
        self.assertNotIn("gateway.controlUi.dangerouslyDisableDeviceAuth", settings)
        self.assertTrue(settings["tools.loopDetection.enabled"])
        self.assertTrue(
            settings["tools.web.fetch.ssrfPolicy.allowRfc2544BenchmarkRange"]
        )

    def test_control_ui_root_is_taken_from_nix_package_environment(self) -> None:
        with patch.dict(
            os.environ,
            {"NIXLOOM_OPENCLAW_CONTROL_UI_ROOT": "/home/yuzujr/.local/state/nixloom/.openclaw/control-ui"},
        ):
            settings = {
                item["path"]: item["value"] for item in managed_settings(self.config)
            }
        self.assertEqual(
            settings["gateway.controlUi.root"],
            "/home/yuzujr/.local/state/nixloom/.openclaw/control-ui",
        )

    def test_sync_control_ui_copies_the_complete_owned_ui(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            src = Path(temporary) / "src"
            src.mkdir()
            (src / "index.html").write_text(
                "<html><body>owned UI</body></html>", encoding="utf-8"
            )
            (src / "app.js").write_text("app", encoding="utf-8")
            # Mirror the Nix store's read-only source layout.
            os.chmod(src, 0o555)
            for f in (src / "index.html", src / "app.js"):
                os.chmod(f, 0o444)
            root = Path(temporary) / "root"
            _sync_control_ui(src, root)
            html = (root / "index.html").read_text(encoding="utf-8")
            self.assertEqual(html, "<html><body>owned UI</body></html>")
            self.assertFalse((root / "nixloom-control-ui.css").exists())
            self.assertFalse((root / "nixloom-control-ui.js").exists())
            self.assertEqual((root / "app.js").stat().st_nlink, 1)

    def test_tavily_provider_is_configured_when_key_exists(self) -> None:
        self.config.value["credentials"]["tavily_api_key"] = "tvly-test"
        with (
            patch("nixloom.openclaw._sync_plugin_path") as sync_plugin_path,
            patch("nixloom.openclaw._set_batch") as set_batch,
            patch.dict(
                "os.environ",
                {"NIXLOOM_OPENCLAW_TAVILY_PLUGIN_PATH": "/plugin/tavily"},
            ),
            patch.object(Path, "is_file", return_value=True),
        ):
            from nixloom.openclaw import _configure_tavily

            _configure_tavily(self.config)
        sync_plugin_path.assert_called_once_with(
            Path("/plugin/tavily"), "/share/nixloom/openclaw-plugin-tavily"
        )
        configured = {
            item["path"]: item["value"] for item in set_batch.call_args.args[0]
        }
        self.assertTrue(configured["plugins.entries.tavily.enabled"])
        self.assertEqual(configured["tools.web.search.provider"], "tavily")

    def test_tavily_provider_is_removed_when_key_is_absent(self) -> None:
        self.config.value["credentials"]["tavily_api_key"] = ""
        with (
            patch("nixloom.openclaw._sync_plugin_path") as sync_plugin_path,
            patch("nixloom.openclaw._unset_paths") as unset_paths,
        ):
            from nixloom.openclaw import _configure_tavily

            _configure_tavily(self.config)
        sync_plugin_path.assert_called_once_with(
            None, "/share/nixloom/openclaw-plugin-tavily"
        )
        unset_paths.assert_called_once_with(TAVILY_SETTINGS)

    def test_disabled_features_converge_to_absence(self) -> None:
        self.config.value["images"]["enabled"] = False
        self.config.value["openclaw"]["workspace"] = ""
        self.config.value["openclaw"]["yuanbao"] = False
        expected_unsets = [
            "gateway.controlUi.dangerouslyDisableDeviceAuth",
            "agents.defaults.workspace",
            *IMAGE_SETTINGS,
        ]
        with (
            patch("nixloom.openclaw._set_batch") as set_batch,
            patch("nixloom.openclaw._unset_paths") as unset_paths,
            patch("nixloom.openclaw._disable_yuanbao") as disable_yuanbao,
            patch("nixloom.openclaw._configure_tavily") as configure_tavily,
        ):
            reconcile_settings(self.config)
        set_batch.assert_called_once()
        unset_paths.assert_called_once_with(expected_unsets)
        disable_yuanbao.assert_called_once_with()
        configure_tavily.assert_called_once_with(self.config)
        self.assertEqual(len(YUANBAO_SETTINGS), 2)
        self.assertEqual(len(YUANBAO_SECRET_SETTINGS), 2)

    def test_config_merge_preserves_unmanaged_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "openclaw.json"
            config_path.write_text(
                '{"unmanaged":{"keep":true},"gateway":{"bind":"lan"}}\n',
                encoding="utf-8",
            )
            with patch.dict(
                os.environ, {"OPENCLAW_CONFIG_PATH": str(config_path)}, clear=True
            ):
                _set_batch([{"path": "gateway.bind", "value": "loopback"}])
                _unset_paths(["gateway.missing"])
            result = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(result["gateway"]["bind"], "loopback")
            self.assertTrue(result["unmanaged"]["keep"])

    def test_run_enables_openclaw_nix_mode_before_gateway_exec(self) -> None:
        def stop_at_exec(*_args: object) -> None:
            self.assertEqual(os.environ["OPENCLAW_NIX_MODE"], "1")
            raise RuntimeError("exec")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = RuntimePaths(
                state=root / "state",
                data=root / "data",
                cache=root / "cache",
                config_dir=root / "config",
                config_file=ROOT / "config.yaml",
                share=None,
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("nixloom.openclaw.reconcile_settings") as reconcile,
                patch("nixloom.openclaw._ensure_token", return_value="token"),
                patch("nixloom.openclaw.os.execvp", side_effect=stop_at_exec),
                self.assertRaisesRegex(RuntimeError, "exec"),
            ):
                run(self.config, paths)
            reconcile.assert_called_once_with(self.config)


if __name__ == "__main__":
    unittest.main()
