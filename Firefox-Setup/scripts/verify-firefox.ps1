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

# Open a random Bhakti channel in Firefox to verify the Google session is alive.
$channelIds = @(
    'UCiMASbpDUjNvy5CJAmfekOw'
    'UCLIryeFjYeiEtpqNETz_Ydg'
    'UCAJcxMaiGu-cjzklR-63ojw'
    'UCuFjc50BSjqeW7AOVmSR7dQ'
    'UCL0cLclH8j_qGjQhnn_5skg'
    'UC31Y8qVbsrRMUt1hbIfvCaw'
    'UC5zCR2OSUvo1g49rkAL8PoQ'
    'UCBAvMHZO3BIfMMhOK9LMOYQ'
    'UC82-0zBQho_hyV10fFAAeQA'
    'UCpSTRmTFY7pCzdeHJwAiAEg'
    'UC1OSbPhj52oW6VM6Odq4uzA'
    'UCT1egsvA08YcdMLiEu1DTRg'
    'UC1qqv4R3RhT5OVMy-E_PciQ'
    'UCJKGP1t3yZMrh1Yc4Afs5rQ'
    'UC7Uo3euG3IA0yBlQyIXDcUA'
    'UCsCY7yimnS3FCIo-SCXD-Zg'
    'UCmX4QOJHAu2vni7nuGmNT5A'
    'UCxghhy9WjHpiO2jixD3t6WQ'
    'UCT3k8uyu8K8r6155o-9shdg'
)

$selectedChannelId = Get-Random -InputObject $channelIds
$youtubeUrl = "https://www.youtube.com/channel/$selectedChannelId/live"
Write-Host "Opening random Bhakti channel in Firefox: $youtubeUrl"
Start-Process -FilePath $FirefoxExecutable -ArgumentList @($youtubeUrl)

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
