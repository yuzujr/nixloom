import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from nixloom.cli import (
    ServiceReport,
    ServiceSpec,
    command_logs,
    command_start,
    command_status,
    command_stop,
)
from nixloom.config import Config, RuntimePaths

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        cls.config = Config.load(cls.paths)
        cls.specs = [
            ServiceSpec(
                "runtime", "nixloom-runtime.service", "http://127.0.0.1:8080/health"
            ),
            ServiceSpec(
                "openclaw", "nixloom-openclaw.service", "http://127.0.0.1:18789/healthz"
            ),
            ServiceSpec(
                "sillytavern", "nixloom-sillytavern.service", "http://127.0.0.1:8000/"
            ),
        ]

    def reports(
        self, active: str = "active", health: str = "ok"
    ) -> list[ServiceReport]:
        sub = "running" if active == "active" else "dead"
        return [ServiceReport(spec, active, sub, health) for spec in self.specs]

    def test_status_combines_systemd_and_endpoint_state_once(self) -> None:
        output = io.StringIO()
        with (
            patch("nixloom.cli._context", return_value=(self.paths, self.config)),
            patch("nixloom.cli._collect_reports", return_value=self.reports()),
            patch("nixloom.cli._running_models", return_value=["qwen (ready)"]),
            redirect_stdout(output),
        ):
            result = command_status(SimpleNamespace(config=None))
        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("NixLoom: ready (3/3 services healthy)", rendered)
        self.assertEqual(rendered.count("runtime"), 1)
        self.assertEqual(rendered.count("openclaw"), 1)
        self.assertEqual(rendered.count("sillytavern"), 1)
        self.assertIn("Model: qwen (ready)", rendered)

    def test_stopped_status_is_actionable_and_nonzero(self) -> None:
        output = io.StringIO()
        with (
            patch("nixloom.cli._context", return_value=(self.paths, self.config)),
            patch(
                "nixloom.cli._collect_reports",
                return_value=self.reports("inactive", "down"),
            ),
            redirect_stdout(output),
        ):
            result = command_status(SimpleNamespace(config=None))
        self.assertEqual(result, 1)
        self.assertIn("NixLoom: stopped", output.getvalue())
        self.assertIn("Start with: nixloom start", output.getvalue())

    def test_start_is_idempotent_when_stack_and_model_are_ready(self) -> None:
        output = io.StringIO()
        with (
            patch("nixloom.cli._context", return_value=(self.paths, self.config)),
            patch("nixloom.cli._service_specs", return_value=self.specs),
            patch("nixloom.cli._collect_reports", return_value=self.reports()),
            patch("nixloom.cli._configured_model_ready", return_value=True),
            patch("nixloom.cli.operations.systemctl") as systemctl,
            redirect_stdout(output),
        ):
            result = command_start(
                SimpleNamespace(config=None, restart=False, dry_run=False)
            )
        self.assertEqual(result, 0)
        systemctl.assert_not_called()
        self.assertIn("already ready", output.getvalue())

    def test_start_waits_for_every_service_before_reporting_ready(self) -> None:
        output = io.StringIO()
        with (
            patch("nixloom.cli._context", return_value=(self.paths, self.config)),
            patch("nixloom.cli._service_specs", return_value=self.specs),
            patch(
                "nixloom.cli._collect_reports",
                side_effect=[self.reports("inactive", "down"), self.reports()],
            ),
            patch("nixloom.cli._configured_model_ready", return_value=False),
            patch("nixloom.cli._wait_for_endpoint") as wait,
            patch("nixloom.cli._warm_model") as warm,
            patch("nixloom.cli.operations.systemctl") as systemctl,
            redirect_stdout(output),
        ):
            result = command_start(
                SimpleNamespace(config=None, restart=False, dry_run=False)
            )
        self.assertEqual(result, 0)
        self.assertEqual(
            systemctl.call_args_list,
            [
                call("start", "nixloom.target"),
                call("start", *(spec.unit for spec in self.specs)),
            ],
        )
        self.assertEqual(
            [call.args[0].name for call in wait.call_args_list],
            [
                "runtime",
                "openclaw",
                "sillytavern",
            ],
        )
        warm.assert_called_once_with(self.config)
        self.assertIn("NixLoom is ready", output.getvalue())

    def test_stop_is_idempotent(self) -> None:
        output = io.StringIO()
        with (
            patch("nixloom.cli._context", return_value=(self.paths, self.config)),
            patch("nixloom.cli._service_specs", return_value=self.specs),
            patch(
                "nixloom.cli._service_report",
                side_effect=self.reports("inactive", "down"),
            ),
            patch("nixloom.cli.operations.systemctl") as systemctl,
            redirect_stdout(output),
        ):
            result = command_stop(SimpleNamespace(config=None))
        self.assertEqual(result, 0)
        systemctl.assert_not_called()
        self.assertIn("already stopped", output.getvalue())

    def test_logs_all_selects_each_service_unit(self) -> None:
        with (
            patch("nixloom.cli._context", return_value=(self.paths, self.config)),
            patch("nixloom.cli._service_specs", return_value=self.specs),
            patch("nixloom.cli.os.execvp") as execvp,
        ):
            command_logs(
                SimpleNamespace(config=None, service="all", lines=25, follow=False)
            )
        command = execvp.call_args.args[1]
        self.assertEqual(command[:4], ["journalctl", "--user", "--lines", "25"])
        for spec in self.specs:
            self.assertIn(spec.unit, command)
        self.assertEqual(command[-1], "--no-pager")


if __name__ == "__main__":
    unittest.main()
