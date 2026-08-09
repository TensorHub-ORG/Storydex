param(
    [string]$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$appLogoPath = Join-Path $RepositoryRoot 'assets\storydex.png'
$androidRes = Join-Path $RepositoryRoot 'apps\android\app\src\main\res'
$webPublic = Join-Path $RepositoryRoot 'apps\android-frontend\public'

foreach ($required in @($appLogoPath, $androidRes, $webPublic)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required branding input is missing: $required"
    }
}

function Convert-WhiteToTransparent {
    param([System.Drawing.Image]$Source)

    $bitmap = New-Object System.Drawing.Bitmap $Source.Width, $Source.Height, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.DrawImage($Source, 0, 0, $Source.Width, $Source.Height)
    }
    finally {
        $graphics.Dispose()
    }

    for ($y = 0; $y -lt $bitmap.Height; $y++) {
        for ($x = 0; $x -lt $bitmap.Width; $x++) {
            $pixel = $bitmap.GetPixel($x, $y)
            $distance = 255 - [Math]::Min($pixel.R, [Math]::Min($pixel.G, $pixel.B))
            if ($distance -le 64) {
                $alpha = if ($distance -le 24) { 0 } else {
                    [Math]::Max(0, [Math]::Min(255, [int][Math]::Round(($distance - 24) / 40.0 * 255)))
                }
                $bitmap.SetPixel($x, $y, [System.Drawing.Color]::FromArgb($alpha, $pixel.R, $pixel.G, $pixel.B))
            }
        }
    }
    return $bitmap
}

function Save-ContainedPng {
    param(
        [System.Drawing.Image]$Source,
        [string]$Destination,
        [int]$Width,
        [int]$Height,
        [double]$Fill = 1.0,
        [System.Drawing.Color]$Background = [System.Drawing.Color]::Transparent
    )

    $bitmap = New-Object System.Drawing.Bitmap $Width, $Height, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.Clear($Background)
        $graphics.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceOver
        $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
        $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality

        $maxWidth = $Width * $Fill
        $maxHeight = $Height * $Fill
        $scale = [Math]::Min($maxWidth / $Source.Width, $maxHeight / $Source.Height)
        $drawWidth = [Math]::Max(1, [int][Math]::Round($Source.Width * $scale))
        $drawHeight = [Math]::Max(1, [int][Math]::Round($Source.Height * $scale))
        $x = [int][Math]::Round(($Width - $drawWidth) / 2)
        $y = [int][Math]::Round(($Height - $drawHeight) / 2)
        $graphics.DrawImage($Source, $x, $y, $drawWidth, $drawHeight)

        $directory = Split-Path -Parent $Destination
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
        $bitmap.Save($Destination, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

$appLogo = [System.Drawing.Image]::FromFile($appLogoPath)
$transparentLogo = Convert-WhiteToTransparent $appLogo
try {
    $legacySizes = [ordered]@{
        'mdpi' = 48
        'hdpi' = 72
        'xhdpi' = 96
        'xxhdpi' = 144
        'xxxhdpi' = 192
    }
    $foregroundSizes = [ordered]@{
        'mdpi' = 108
        'hdpi' = 162
        'xhdpi' = 216
        'xxhdpi' = 324
        'xxxhdpi' = 432
    }

    foreach ($density in $legacySizes.Keys) {
        $size = $legacySizes[$density]
        $directory = Join-Path $androidRes "mipmap-$density"
        Save-ContainedPng $appLogo (Join-Path $directory 'ic_launcher.png') $size $size 0.78 ([System.Drawing.Color]::White)
        Save-ContainedPng $appLogo (Join-Path $directory 'ic_launcher_round.png') $size $size 0.78 ([System.Drawing.Color]::White)
    }

    foreach ($density in $foregroundSizes.Keys) {
        $size = $foregroundSizes[$density]
        $mipmapDirectory = Join-Path $androidRes "mipmap-$density"
        $drawableDirectory = Join-Path $androidRes "drawable-$density"
        # Android scales adaptive foregrounds beyond the final mask. Keep the full mark deliberately loose.
        Save-ContainedPng $transparentLogo (Join-Path $mipmapDirectory 'ic_launcher_foreground.png') $size $size 0.52
        Save-ContainedPng $transparentLogo (Join-Path $drawableDirectory 'ic_foreground.png') $size $size 0.72
    }

    Save-ContainedPng $transparentLogo (Join-Path $androidRes 'drawable-nodpi\coomi_logo.png') 512 512
    Save-ContainedPng $transparentLogo (Join-Path $androidRes 'drawable\botdrop_logo.png') 256 256
    Save-ContainedPng $appLogo (Join-Path $androidRes 'drawable\banner.png') 320 180 0.86 ([System.Drawing.Color]::White)

    Save-ContainedPng $appLogo (Join-Path $webPublic 'favicon.png') 64 64 0.78 ([System.Drawing.Color]::White)
    Save-ContainedPng $appLogo (Join-Path $webPublic 'apple-touch-icon.png') 180 180 0.78 ([System.Drawing.Color]::White)
    Save-ContainedPng $transparentLogo (Join-Path $webPublic 'coomi-logo.png') 256 256
    Save-ContainedPng $transparentLogo (Join-Path $webPublic 'storydex-icon.png') 256 256
}
finally {
    $appLogo.Dispose()
    $transparentLogo.Dispose()
}

Write-Output 'Storydex Android and Web branding assets regenerated.'
