[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$FirefoxExecutable,

    [Parameter(Mandatory = $true)]
    [string]$ScreenshotDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not (Test-Path -LiteralPath $FirefoxExecutable -PathType Leaf)) {
    throw "Firefox executable was not found: $FirefoxExecutable"
}

New-Item -ItemType Directory -Path $ScreenshotDirectory -Force | Out-Null

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Save-Screenshot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $screen = [System.Windows.Forms.Screen]::PrimaryScreen
    if ($null -eq $screen) {
        throw 'No primary screen is available for the screenshot.'
    }

    $outputPath = Join-Path $ScreenshotDirectory $Name
    $bounds = $screen.Bounds
    $bitmap = [System.Drawing.Bitmap]::new($bounds.Width, $bounds.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)

    try {
        $graphics.CopyFromScreen(
            $bounds.Location,
            [System.Drawing.Point]::Empty,
            $bounds.Size
        )
        $bitmap.Save(
            $outputPath,
            [System.Drawing.Imaging.ImageFormat]::Jpeg
        )
    }
    finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }

    Write-Host "Saved screenshot: $outputPath"
}

# Open YouTube in Firefox to verify the Google session is alive
Write-Host "Opening YouTube in Firefox to verify Google sign-in..."
Start-Process -FilePath $FirefoxExecutable -ArgumentList @('https://www.youtube.com/channel/UCYn6UEtQ771a_OWSiNBoG8w/live')

Write-Host 'Waiting 5 seconds for YouTube to load...'
Start-Sleep -Seconds 5

$firefoxWindow = Get-Process -Name firefox -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } |
    Sort-Object StartTime -Descending |
    Select-Object -First 1

if ($null -eq $firefoxWindow) {
    throw 'Could not find a visible Firefox window to start YouTube playback.'
}

$windowShell = New-Object -ComObject WScript.Shell
if (-not $windowShell.AppActivate($firefoxWindow.Id)) {
    throw 'Could not activate the Firefox window to start YouTube playback.'
}

Start-Sleep -Milliseconds 500
Write-Host 'Sending Space key to start YouTube playback...'
[System.Windows.Forms.SendKeys]::SendWait(' ')

Write-Host 'Waiting 30 seconds before continuing...'
Start-Sleep -Seconds 30

Save-Screenshot -Name '01-youtube-sign-in-check.jpg'

# Close Firefox
Write-Host 'Closing Firefox...'
Get-Process -Name firefox -ErrorAction SilentlyContinue |
    Stop-Process -Force
Start-Sleep -Seconds 3

if (Get-Process -Name firefox -ErrorAction SilentlyContinue) {
    Write-Host 'Warning: Firefox is still running after stop attempt.'
}
else {
    Write-Host 'Firefox closed successfully.'
}
