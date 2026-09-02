from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class WindowsLocalControlTests(unittest.TestCase):
    def test_proxy_gate_waits_and_notifies_instead_of_failing_immediately(self) -> None:
        script = (ROOT / "scripts" / "assert-direct-network.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("MaxWaitSeconds", script)
        self.assertIn("请关闭代理，即将开始优选", script)
        self.assertIn("Start-Sleep", script)
        self.assertIn("notify-user.ps1", script)

    def test_handoff_mirror_download_is_bound_to_cloud_sha256(self) -> None:
        script = (ROOT / "scripts" / "sync-cloud-handoff.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("ExpectedSha256", script)
        self.assertIn("Get-FileHash", script)
        self.assertIn("gh-proxy.com", script)
        self.assertIn("ghfast.top", script)
        self.assertIn("gh.ddlc.top", script)
        self.assertNotIn("ghproxy.net", script)

        workflow = (ROOT / ".github" / "workflows" / "update.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("handoff_sha256", workflow)
        self.assertIn("handoff_ref", workflow)
        self.assertIn("-Branch \"${{ needs.cloud-prepare.outputs.handoff_ref }}\"", workflow)
        self.assertIn("sync-cloud-handoff.ps1", workflow)

    def test_windows_job_uses_installed_app_and_bypass_shell(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "update.yml").read_text(
            encoding="utf-8"
        )
        local_section = workflow.split("  local-select:", 1)[1].split(
            "  cloud-publish:", 1
        )[0]
        self.assertIn("Validate the installed local application", local_section)
        self.assertIn(r"Noode-CG-Local\app", local_section)
        self.assertNotIn("uses: actions/checkout", local_section)
        self.assertIn("-ExecutionPolicy Bypass -File", local_section)
        self.assertIn("PSExecutionPolicyPreference: Bypass", local_section)
        self.assertNotIn("shell: powershell\n", local_section)

    def test_windows_job_uses_prepared_runner_python_without_setup_action(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "update.yml").read_text(
            encoding="utf-8"
        )
        local_section = workflow.split("  local-select:", 1)[1].split(
            "  cloud-publish:", 1
        )[0]
        self.assertNotIn("actions/setup-python", local_section)
        self.assertNotIn("-m pip install", local_section)
        self.assertIn(r"runtime\python\python.exe", local_section)
        self.assertIn("NOODE_PYTHON", local_section)
        self.assertIn("import yaml", local_section)

        installer = (ROOT / "scripts" / "install-windows-controller.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("Prepare-RunnerPython", installer)
        self.assertIn("NETWORK SERVICE", installer)
        self.assertIn("runtime", installer)

    def test_windows_powershell_reads_python_json_as_utf8(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "update.yml").read_text(
            encoding="utf-8"
        )
        utf8_read = (
            'Get-Content -Raw -Encoding UTF8 -LiteralPath "output/health.json" '
            '| ConvertFrom-Json'
        )
        self.assertEqual(workflow.count(utf8_read), 3)
        self.assertNotIn(
            'Get-Content -Raw -LiteralPath "output/health.json" | ConvertFrom-Json',
            workflow,
        )

    def test_replenish_dispatch_explicitly_targets_repository_without_checkout(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "update.yml").read_text(
            encoding="utf-8"
        )
        replenish = workflow.split("  replenish-cloud-pool:", 1)[1]
        self.assertNotIn("uses: actions/checkout", replenish)
        self.assertIn('--repo "${GITHUB_REPOSITORY}"', replenish)
        self.assertIn("actions: write", replenish)

    def test_self_dispatched_replenishment_does_not_cancel_parent_run(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "update.yml").read_text(
            encoding="utf-8"
        )
        concurrency = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]
        self.assertIn("cancel-in-progress: false", concurrency)
        self.assertNotIn("cancel-in-progress: true", concurrency)

    def test_windows_job_does_not_fetch_or_push_github_directly(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "update.yml").read_text(
            encoding="utf-8"
        )
        local_section = workflow.split("  local-select:", 1)[1].split(
            "  cloud-publish:", 1
        )[0]
        self.assertNotIn("git fetch", local_section)
        self.assertNotIn("git pull", local_section)
        self.assertNotIn("git push", local_section)
        self.assertNotIn("github.com/$env:GITHUB_REPOSITORY", local_section)
        self.assertIn(r"Noode-CG-Local\app", local_section)
        self.assertIn("result_payload_0", local_section)
        self.assertIn("cloud-publish:", workflow)
        self.assertIn("result_payload.py unpack", workflow)

        installer = (ROOT / "scripts" / "install-windows-controller.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("Install-LocalApplication", installer)

    def test_successful_cloud_publish_removes_completed_cycle_state(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "update.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "rm -f data/handoff/local-qualified.json.gz "
            "data/handoff/local-attempted-ips.txt.gz",
            workflow,
        )

    def test_dashboard_launcher_is_visible_and_manual_start_triggers_cloud_workflow(self) -> None:
        manual_path = ROOT / "windows-controller" / "manual-start.ps1"
        self.assertTrue(
            manual_path.read_bytes().startswith(b"\xef\xbb\xbf"),
            "Windows PowerShell 5.1 requires a UTF-8 BOM for this localized script",
        )
        manual = manual_path.read_text(
            encoding="utf-8-sig"
        )
        launcher = (ROOT / "windows-controller" / "开始云端和本地优选.cmd").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("workflow run $workflow", manual)
        self.assertIn("jachjkl/Noode-CG", manual)
        self.assertIn('$watchArguments = @("run", "watch"', manual)
        self.assertIn("foreach ($run in @($parsed))", manual)
        self.assertNotIn("return @($output | ConvertFrom-Json)", manual)
        self.assertIn("run watch --help", manual)
        self.assertIn('$watchArguments += "--compact"', manual)
        self.assertNotIn("$Repository --compact --exit-status", manual)
        self.assertIn("launch-dashboard.ps1", launcher)
        self.assertNotIn("/min", launcher.lower())
        self.assertNotIn("WindowStyle Minimized", launcher)

        dashboard_launcher = (
            ROOT / "windows-controller" / "launch-dashboard.ps1"
        ).read_text(encoding="utf-8-sig")
        self.assertIn('"--no-start"', dashboard_launcher)
        self.assertIn('if ($NoBrowser)', dashboard_launcher)
        self.assertNotIn("EncodedCommand", dashboard_launcher)

    def test_notifications_use_hidden_user_watcher_and_runtime_is_cleaned(self) -> None:
        watcher = (ROOT / "windows-controller" / "notification-watcher.ps1").read_text(
            encoding="utf-8-sig"
        )
        notifier = (ROOT / "scripts" / "notify-user.ps1").read_text(
            encoding="utf-8-sig"
        )
        runtime = (ROOT / "scripts" / "local-runtime.ps1").read_text(
            encoding="utf-8-sig"
        )
        workflow = (ROOT / ".github" / "workflows" / "update.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("ToastNotificationManager", watcher)
        self.assertNotIn("NotifyIcon", watcher)
        self.assertIn("NoodeCGNotificationWatcher", watcher)
        self.assertIn("notifications", notifier)
        self.assertIn("Save", runtime)
        self.assertIn("Restore", runtime)
        self.assertIn("ClearPending", runtime)
        self.assertIn("ClearRuntime", runtime)
        self.assertIn("优选已完成", workflow)
        self.assertIn("local-runtime.ps1 -Mode Save", workflow)
        self.assertIn("local-runtime.ps1 -Mode ClearRuntime", workflow)

    def test_installer_targets_requested_d_drive_folder_and_startup(self) -> None:
        installer = (ROOT / "scripts" / "install-windows-controller.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn(r"D:\桌面\软件\Noode-CG-Local", installer)
        self.assertIn("Startup", installer)
        self.assertIn("notification-watcher.ps1", installer)
        self.assertIn("manual-start.ps1", installer)
        self.assertIn("launch-dashboard.ps1", installer)
        self.assertIn("dashboard_server.py", installer)
        self.assertIn('(Join-Path $source "dashboard")', installer)
        self.assertIn("UTF8Encoding($true)", installer)

    def test_installer_saves_unpublished_runtime_before_replacing_app(self) -> None:
        installer = (ROOT / "scripts" / "install-windows-controller.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("function Save-ExistingRuntimeState", installer)
        self.assertIn(
            "Save-ExistingRuntimeState\nInstall-LocalApplication\nPrepare-RunnerPython",
            installer.replace("\r\n", "\n"),
        )

    def test_installer_preserves_saved_local_rules(self) -> None:
        installer = (ROOT / "scripts" / "install-windows-controller.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn('$existingRules = Join-Path $appRoot "data\\local-rules.json"', installer)
        self.assertIn("已保留本机自定义优选规则", installer)


if __name__ == "__main__":
    unittest.main()
