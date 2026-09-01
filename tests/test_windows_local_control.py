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
        self.assertIn("sync-cloud-handoff.ps1", workflow)

    def test_windows_checkout_avoids_checkout_submodule_shell_bug(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "update.yml").read_text(
            encoding="utf-8"
        )
        local_section = workflow.split("  local-select:", 1)[1].split(
            "  replenish-cloud-pool:", 1
        )[0]
        self.assertIn("Checkout on Windows without Git submodule shell", local_section)
        self.assertIn("git fetch --no-tags", local_section)
        self.assertIn("git checkout --force", local_section)
        self.assertNotIn("uses: actions/checkout", local_section)
        self.assertIn("-ExecutionPolicy Bypass -File", local_section)
        self.assertIn("PSExecutionPolicyPreference: Bypass", local_section)
        self.assertNotIn("shell: powershell\n", local_section)

    def test_manual_controller_is_visible_and_triggers_cloud_workflow(self) -> None:
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
        self.assertIn("manual-start.ps1", launcher)
        self.assertNotIn("/min", launcher.lower())
        self.assertNotIn("WindowStyle Minimized", launcher)

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
        self.assertIn("UTF8Encoding($true)", installer)


if __name__ == "__main__":
    unittest.main()
