param([string]$Version)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$sourceVersion = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw).Trim()
if ($Version -and $Version -ne $sourceVersion) { throw 'Release version must match VERSION.' }
$Version = $sourceVersion
$destination = Join-Path $root 'dist'
New-Item -ItemType Directory -Force -Path $destination | Out-Null
$archive = Join-Path $destination "ascent-streamer-$Version.zip"
$files = @('VERSION','viewer.py','legacy_receiver.py','ascent-viewer.service','playback-baseline.json','README.md','PUBLIC_RELEASE_SCRUB_REPORT.md','UI-CANDIDATE.txt','INSTALL.md','VALIDATION.md','INSPECTION.md','test_viewer.py','test_capture_rtsp.py','test_headless.py','test_inspect_h264.py','capture_rtsp.py','inspect_h264.py','deploy.ps1','web','assets','scripts')
$paths = $files | ForEach-Object { Join-Path $root $_ }
Add-Type -AssemblyName System.IO.Compression.FileSystem
Add-Type -AssemblyName System.IO.Compression
$entries = $paths | ForEach-Object {
    if (Test-Path -LiteralPath $_ -PathType Container) { Get-ChildItem -LiteralPath $_ -Recurse -File }
    else { Get-Item -LiteralPath $_ }
} | Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -ne '.pyc' } | Sort-Object FullName
$temporary = $archive + '.tmp'
if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
$zip = [IO.Compression.ZipFile]::Open($temporary, [IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($entry in $entries) {
        $relative = $entry.FullName.Substring($root.Length + 1).Replace('\','/')
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $entry.FullName, $relative, [IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
} finally { $zip.Dispose() }
Move-Item -LiteralPath $temporary -Destination $archive -Force
$digest = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLower()
[IO.File]::WriteAllText($archive + '.sha256', "$digest  $(Split-Path $archive -Leaf)" + [Environment]::NewLine)
Write-Output $archive
Write-Output "SHA256: $digest"
