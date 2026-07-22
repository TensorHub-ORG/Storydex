param(
  [Parameter(Mandatory = $true)][string]$InstallerPath,
  [Parameter(Mandatory = $true)][string]$AppPath,
  [Parameter(Mandatory = $true)][string]$LockPath,
  [Parameter(Mandatory = $true)][int]$ParentPid,
  [Parameter(Mandatory = $true)][string]$LogPath,
  [switch]$TestMode
)

$ErrorActionPreference = "Stop"
$script:ExitCode = 1
$script:Form = $null
$script:FailureHandled = $false

function Write-UpdateLog([string]$Message) {
  $line = "$(Get-Date -Format o) $Message"
  Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

function Set-InstallLock([string]$State) {
  $payload = @{ state = $State; helperPid = $PID; installer = $InstallerPath; updatedAt = (Get-Date).ToUniversalTime().ToString("o") } | ConvertTo-Json -Compress
  $temporaryPath = "$LockPath.$PID.tmp"
  $backupPath = "$LockPath.$PID.bak"
  try {
    [IO.File]::WriteAllText($temporaryPath, $payload, [Text.UTF8Encoding]::new($false))
    if ([IO.File]::Exists($LockPath)) {
      [IO.File]::Replace($temporaryPath, $LockPath, $backupPath)
    } else {
      [IO.File]::Move($temporaryPath, $LockPath)
    }
  } finally {
    Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
  }
}

function Remove-InstallLock {
  Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
}

function Handle-UpdateFailure([string]$Message) {
  if ($script:FailureHandled) { return }
  $script:FailureHandled = $true
  $script:ExitCode = 1
  try { Write-UpdateLog "Installation failed: $Message" } catch {}
  try { Set-InstallLock "failed" } catch {}
  if ($script:Form) {
    try {
      $progress.Style = "Blocks"; $progress.Value = 0
      $title.Text = "Storydex update failed"
      $detail.Text = "$Message`nThe existing version is preserved. Try again later or use the full installer."
      [Windows.Forms.Application]::DoEvents()
      if (-not $TestMode) { [Windows.Forms.MessageBox]::Show($detail.Text, "Storydex Update", "OK", "Error") | Out-Null }
    } catch {}
    try { $script:Form.Close() } catch {}
  }
  Remove-InstallLock
}

try {
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing

  $form = New-Object Windows.Forms.Form
  $script:Form = $form
  $form.Text = "Storydex Update"
  $form.Width = 480
  $form.Height = 190
  $form.StartPosition = "CenterScreen"
  $form.FormBorderStyle = "FixedDialog"
  $form.MaximizeBox = $false
  $form.MinimizeBox = $false
  if ($TestMode) { $form.ShowInTaskbar = $false; $form.Opacity = 0 }

  $title = New-Object Windows.Forms.Label
  $title.Left = 24; $title.Top = 20; $title.Width = 420; $title.Height = 28
  $title.Font = New-Object Drawing.Font("Microsoft YaHei UI", 12, [Drawing.FontStyle]::Bold)
  $title.Text = "Preparing the Storydex update"
  $form.Controls.Add($title)

  $detail = New-Object Windows.Forms.Label
  $detail.Left = 24; $detail.Top = 58; $detail.Width = 420; $detail.Height = 38
  $detail.Font = New-Object Drawing.Font("Microsoft YaHei UI", 9)
  $detail.Text = "Waiting for Storydex to exit safely. Do not start the app again."
  $form.Controls.Add($detail)

  $progress = New-Object Windows.Forms.ProgressBar
  $progress.Left = 24; $progress.Top = 108; $progress.Width = 420; $progress.Height = 18
  $progress.Style = "Marquee"; $progress.MarqueeAnimationSpeed = 28
  $form.Controls.Add($progress)

  $form.Add_Shown({
    try {
      Set-InstallLock "waiting-for-app-exit"
      Write-UpdateLog "Helper started. parent=$ParentPid installer=$InstallerPath"
      $deadline = if ($TestMode) { (Get-Date).AddSeconds(1) } else { (Get-Date).AddMinutes(2) }
      while ((Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
        [Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 200
      }
      if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
        throw "Storydex did not exit within the allowed time. Installation was cancelled."
      }

      $title.Text = "Installing Storydex"
      $detail.Text = "The installer is open. Do not start Storydex until installation is complete."
      Set-InstallLock "installing"
      [Windows.Forms.Application]::DoEvents()
      if ([IO.Path]::GetExtension($InstallerPath) -ieq ".cmd") {
        $installer = Start-Process -FilePath $env:ComSpec -ArgumentList @("/d", "/c", $InstallerPath, "--updated") -PassThru -WindowStyle Hidden
      } else {
        $installer = Start-Process -FilePath $InstallerPath -ArgumentList "--updated" -PassThru
      }
      if (-not $installer.WaitForExit(900000)) {
        try { $installer.Kill() } catch {}
        throw "Installation timed out after 15 minutes."
      }
      if ($installer.ExitCode -ne 0) { throw "Installer exit code: $($installer.ExitCode)" }

      Set-InstallLock "completed"
      Write-UpdateLog "Installer completed successfully."
      $progress.Style = "Blocks"; $progress.Value = 100
      $title.Text = "Storydex update completed"
      $detail.Text = "The new version is installed. Start Storydex now?"
      [Windows.Forms.Application]::DoEvents()
      $choice = if ($TestMode) { [Windows.Forms.DialogResult]::No } else { [Windows.Forms.MessageBox]::Show($detail.Text, "Storydex Update", "YesNo", "Information") }
      Remove-InstallLock
      if ($choice -eq [Windows.Forms.DialogResult]::Yes) {
        Start-Process -FilePath $AppPath | Out-Null
      }
      $script:ExitCode = 0
      $form.Close()
    } catch {
      Handle-UpdateFailure $_.Exception.Message
    }
  })

  [Windows.Forms.Application]::Run($form)
} catch {
  Handle-UpdateFailure $_.Exception.Message
} finally {
  if ($script:ExitCode -ne 0) { Remove-InstallLock }
}
exit $script:ExitCode
