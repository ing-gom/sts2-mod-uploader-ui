# Creates a Desktop shortcut that launches the STS2 Mod Uploader UI.
# The shortcut points at launch.bat in this folder and uses icon.ico.
# If icon.ico is missing, it is generated with make_icon.py first.

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$launcher = Join-Path $here 'launch.bat'
$icon     = Join-Path $here 'icon.ico'

if (-not (Test-Path $launcher)) {
    Write-Error "launch.bat not found next to this script ($launcher)."
    exit 1
}

# Generate the icon on the fly if it is not committed / was deleted.
if (-not (Test-Path $icon)) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
    if ($py) {
        Write-Host "icon.ico missing - generating with make_icon.py ..."
        & $py.Source (Join-Path $here 'make_icon.py') | Out-Null
    }
}

$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop 'STS2 Mod Uploader.lnk'

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnkPath)
$sc.TargetPath        = $launcher
$sc.WorkingDirectory  = $here
$sc.Description        = 'Launch the STS2 Mod Uploader UI dashboard'
if (Test-Path $icon) { $sc.IconLocation = "$icon,0" }
$sc.Save()

Write-Host "Created desktop shortcut:"
Write-Host "  $lnkPath"
