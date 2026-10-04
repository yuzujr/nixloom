import copy
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from nixloom import dsh
from nixloom.config import Config, ConfigError, RuntimePaths
from nixloom.operations import create_backup

ROOT = Path(__file__).resolve().parents[1]


class DshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = RuntimePaths(
            root / "state",
            root / "data",
            root / "cache",
            root / "config",
            ROOT / "config.yaml",
            None,
        )
        self.config = Config.load(self.paths)

    def fake_install(self) -> None:
        app = dsh.app_directory(self.config, self.paths)
        entry = app / "node_modules/@deepseek-ai/dsh/lib/bin.js"
        entry.parent.mkdir(parents=True)
        entry.touch()
        modules = app / "node_modules"
        terminal = modules / "@deepseek-ai/dsh-terminal-bash/lib/index.js"
        terminal.parent.mkdir(parents=True)
        terminal.write_text('const DEFAULT_BASH_SHELL = "/bin/bash";')
        controller = modules / "@deepseek-ai/dsh-api-workspace-controller/lib/index.js"
        controller.parent.mkdir(parents=True)
        controller.write_text(
            "async function defaultWorkspaceDirectory(documentsDirectory, signal, internals = {}) {\n}\n"
        )
        connection = modules / "@deepseek-ai/dsh-client-connection/lib/index.js"
        connection.parent.mkdir(parents=True)
        connection.write_text(
            "\tisAuthenticated(request) {\n\t\tconst authority = requestAuthority(request.headers);\n}\n"
            "authenticatedUrl(baseUrl) {\n\t\tconst url = new URL(baseUrl);\n}\n"
        )
        (app / "node_modules/dsh-image-gen").mkdir()
        profile = self.paths.state / ".dsh/profiles/web"
        profile.mkdir(parents=True)
        (profile / "package.json").write_text(
            json.dumps(
                {
                    "dependencies": {},
                    "dsh": {
                        "profile": {
                            "bundles": [
                                "@deepseek-ai/dsh-base",
                                "@deepseek-ai/dsh-web-app",
                                "user-plugin",
                            ]
                        }
                    },
                }
            )
        )

    def test_settings_preserve_other_providers_and_plugin_rows(self) -> None:
        target = self.paths.state / "patch.yml"
        target.parent.mkdir()
        user = [
            {
                "id": "llm-pi-ai",
                "config": {
                    "providers": {"remote": {"baseURL": "https://example.com/v1"}},
                    "userOption": True,
                },
            },
            {"id": "theme", "config": {"color": "blue"}},
        ]
        target.write_text(yaml.safe_dump(user))
        dsh.sync_settings(self.config, target)
        result = yaml.safe_load(target.read_text())
        providers = result[0]["config"]["providers"]
        self.assertEqual(providers["remote"], user[0]["config"]["providers"]["remote"])
        self.assertTrue(result[0]["config"]["userOption"])
        self.assertEqual(result[1], user[1])
        self.assertEqual(providers["nixloom"]["models"][0]["contextWindow"], 131072)
        self.assertEqual(
            providers["nixloom"]["compat"]["thinkingFormat"], "qwen-chat-template"
        )
        first = target.read_text()
        dsh.sync_settings(self.config, target)
        self.assertEqual(target.read_text(), first)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_prepare_connects_image_plugin_and_preserves_user_bundles(self) -> None:
        self.fake_install()
        with patch("nixloom.dsh.subprocess.run") as run:
            dsh.prepare(self.config, self.paths)
        profile = self.paths.state / ".dsh/profiles/web"
        manifest = json.loads((profile / "package.json").read_text())
        self.assertIn("user-plugin", manifest["dsh"]["profile"]["bundles"])
        self.assertIn("dsh-image-gen", manifest["dsh"]["profile"]["bundles"])
        self.assertEqual(
            (profile / "node_modules/dsh-image-gen").resolve(),
            dsh.app_directory(self.config, self.paths) / "node_modules/dsh-image-gen",
        )
        rows = yaml.safe_load((profile / "cordis.patch.yml").read_text())
        image = next(row["config"] for row in rows if row.get("id") == "image-gen")
        self.assertEqual(image["comfyuiBaseURL"], "http://127.0.0.1:8188")
        self.assertEqual(image["provider"], "comfyui")
        self.assertEqual(
            {x["name"] for x in image["comfyuiWorkflows"]}, {"z-image", "klein-edit"}
        )
        self.assertEqual(run.call_args.kwargs["cwd"], self.paths.data / "dsh/workspace")
        self.assertEqual(
            run.call_args.kwargs["env"]["DSH_HOME"], str(self.paths.state / ".dsh")
        )

    def test_disabling_images_removes_only_managed_bundle_and_patch(self) -> None:
        self.fake_install()
        with patch("nixloom.dsh.subprocess.run"):
            dsh.prepare(self.config, self.paths)
            self.config.value["images"]["enabled"] = False
            dsh.prepare(self.config, self.paths)
        profile = self.paths.state / ".dsh/profiles/web"
        bundles = json.loads((profile / "package.json").read_text())["dsh"]["profile"][
            "bundles"
        ]
        self.assertIn("user-plugin", bundles)
        self.assertNotIn("dsh-image-gen", bundles)
        manifest = json.loads((profile / "package.json").read_text())
        self.assertNotIn("dsh-image-gen", manifest["dependencies"])
        self.assertFalse((profile / "node_modules/dsh-image-gen").is_symlink())
        insertions = [
            item
            for row in yaml.safe_load((profile / "cordis.patch.yml").read_text())
            for item in row.get("insert", [])
        ]
        self.assertEqual(
            [item["id"] for item in insertions], ["nixloom-tavily", "nixloom-workspace"]
        )
        rows = yaml.safe_load((profile / "cordis.patch.yml").read_text())
        self.assertNotIn("image-gen", [row.get("id") for row in rows])

    def test_startup_does_not_prepare_or_modify_packages(self) -> None:
        self.fake_install()
        with (
            patch("nixloom.dsh.require_prepared") as verify,
            patch("nixloom.dsh.prepare") as prepare,
            patch("nixloom.dsh.os.chdir"),
            patch("nixloom.dsh.os.execvpe") as execute,
            patch.dict("os.environ", {"NIXLOOM_DSH_TAILNET": "0"}),
        ):
            dsh.run(self.config, self.paths)
        verify.assert_called_once_with(self.config, self.paths)
        prepare.assert_not_called()
        self.assertEqual(
            execute.call_args.args[2]["NIXLOOM_DSH_WORKSPACE"],
            str(self.paths.data / "dsh/workspace"),
        )

    def test_tailnet_grants_only_owner_devices(self) -> None:
        status = {
            "BackendState": "Running",
            "Self": {
                "UserID": 1,
                "DNSName": "laptop.example.ts.net.",
                "TailscaleIPs": ["100.64.0.2"],
            },
            "Peer": {
                "phone": {"UserID": 1, "TailscaleIPs": ["100.64.0.3"]},
                "shared": {"UserID": 2, "TailscaleIPs": ["100.64.0.4"]},
            },
        }
        with patch(
            "nixloom.dsh.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, json.dumps(status)),
        ):
            identity = dsh.tailnet_identity()
        self.assertEqual(identity["peers"], ["100.64.0.2", "100.64.0.3"])
        self.assertEqual(identity["hostname"], "laptop.example.ts.net")

    def test_tailnet_accepts_short_and_full_magicdns_names(self) -> None:
        with (
            patch("nixloom.dsh.require_prepared"),
            patch("nixloom.dsh.os.chdir"),
            patch("nixloom.dsh.os.execvpe") as execute,
            patch.dict("os.environ", {"NIXLOOM_DSH_TAILNET": "1"}),
            patch(
                "nixloom.dsh.tailnet_identity",
                return_value={
                    "hostname": "laptop.example.ts.net",
                    "addresses": ["100.64.0.2"],
                    "peers": ["100.64.0.3"],
                },
            ),
        ):
            dsh.run(self.config, self.paths)
        hosts = ["laptop.example.ts.net:3080", "laptop:3080", "100.64.0.2:3080"]
        command_args = execute.call_args.args[1]
        self.assertEqual(
            command_args[command_args.index("--trusted-host") + 1 :], hosts
        )
        self.assertEqual(
            json.loads(execute.call_args.args[2]["NIXLOOM_DSH_TAILNET_HOSTS"]), hosts
        )

    def test_missing_installation_is_actionable_and_does_not_download(self) -> None:
        with (
            patch("nixloom.dsh.subprocess.run") as run,
            self.assertRaisesRegex(ConfigError, "Home Manager activation"),
        ):
            dsh.prepare(self.config, self.paths)
        run.assert_not_called()

    def test_failed_install_does_not_publish_a_partial_release(self) -> None:
        with (
            patch(
                "nixloom.dsh.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "npm"),
            ),
            redirect_stdout(io.StringIO()),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            dsh.install(self.config, self.paths)
        self.assertFalse(dsh.app_directory(self.config, self.paths).exists())
        self.assertFalse(
            any(dsh.app_directory(self.config, self.paths).parent.glob(".install-*"))
        )

    def test_version_and_workspace_are_validated_before_install(self) -> None:
        for key, value in [
            ("version", "latest"),
            ("version", "../../escape"),
            ("workspace", "relative/path"),
        ]:
            config = Config(copy.deepcopy(self.config.value), self.config.path)
            config.value["dsh"][key] = value
            with self.assertRaises(ConfigError):
                config.validate()

    def test_backup_keeps_dsh_sessions_without_npm_dependencies(self) -> None:
        home = self.paths.state / ".dsh"
        (home / "sessions").mkdir(parents=True)
        (home / "sessions/chat.jsonl").write_text("chat\n")
        (home / "profiles/web/node_modules/example").mkdir(parents=True)
        (home / "profiles/web/node_modules/example/package.json").write_text("{}")
        archive = create_backup(self.config, self.paths, self.paths.state / "backups")
        with tarfile.open(archive) as backup:
            names = backup.getnames()
        self.assertIn("state/.dsh/sessions/chat.jsonl", names)
        self.assertFalse(any("node_modules" in name for name in names))


if __name__ == "__main__":
    unittest.main()
