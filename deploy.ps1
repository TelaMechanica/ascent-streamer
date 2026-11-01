param([Parameter(Mandatory=$true)][string]$PiAddress, [Parameter(Mandatory=$true)][string]$PiHostKey, [string]$PiPassword)
$ErrorActionPreference = 'Stop'
if (-not $PiPassword) { throw 'Supply -PiPassword for this local SSH deployment.' }
$remoteScript = "import pathlib,base64,subprocess,datetime,shutil" + [Environment]::NewLine
$remoteScript += "backup=pathlib.Path('/var/backups/ascent-deploy-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'));backup.mkdir(mode=0o700)" + [Environment]::NewLine
$deploymentEntries = @(
    @('viewer.py','/opt/ascent-viewer/viewer.py'),
    @('legacy_receiver.py','/opt/ascent-viewer/legacy_receiver.py'),
    @('VERSION','/opt/ascent-viewer/VERSION'),
    @('ascent-viewer.service','/etc/systemd/system/ascent-viewer.service'),
    @('web/index.html','/opt/ascent-viewer/web/index.html'),
    @('capture_rtsp.py','/opt/ascent-viewer/capture_rtsp.py'),
    @('inspect_h264.py','/opt/ascent-viewer/inspect_h264.py'),
    @('scripts/recover-setup.py','/usr/local/sbin/ascent-recover-setup'),
    @('README.md','/opt/ascent-viewer/README.md'),
    @('VALIDATION.md','/opt/ascent-viewer/VALIDATION.md'),
    @('INSPECTION.md','/opt/ascent-viewer/INSPECTION.md')
)
foreach ($entry in $deploymentEntries) {
    $encoded = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $PSScriptRoot $entry[0])))
    $remoteScript += "p=pathlib.Path('$($entry[1])');p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,backup/p.name) if p.exists() else None" + [Environment]::NewLine
    $remoteScript += "pathlib.Path('$($entry[1])').write_bytes(base64.b64decode('$encoded'))" + [Environment]::NewLine
}
foreach ($assetName in @('waiting-vtx.png', 'setup.png')) {
    $stagedPath = "/tmp/ascent-deploy-$assetName"
    & pscp -batch -hostkey $PiHostKey -pw $PiPassword (Join-Path $PSScriptRoot "assets/$assetName") "streamer@${PiAddress}:$stagedPath"
    if ($LASTEXITCODE -ne 0) { throw "Asset transfer failed: $assetName" }
    $remoteScript += "p=pathlib.Path('/opt/ascent-viewer/$assetName');shutil.copy2(p,backup/p.name) if p.exists() else None;shutil.copyfile('$stagedPath',p);p.chmod(0o644);pathlib.Path('$stagedPath').unlink()" + [Environment]::NewLine
}
$remoteScript += "subprocess.run(['python3','-m','py_compile','/opt/ascent-viewer/viewer.py'],check=True)" + [Environment]::NewLine
$headlessPayload = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'scripts/configure_headless.py')))
$remoteScript += "p=pathlib.Path('/usr/local/sbin/ascent-configure-headless');shutil.copy2(p,backup/p.name) if p.exists() else None;p.write_bytes(base64.b64decode('$headlessPayload'));p.chmod(0o755)" + [Environment]::NewLine
$remoteScript += "subprocess.run(['systemctl','daemon-reload'],check=True)" + [Environment]::NewLine
$remoteScript += "pathlib.Path('/usr/local/sbin/ascent-recover-setup').chmod(0o755)" + [Environment]::NewLine
$remoteScript += "pathlib.Path(__file__).unlink()" + [Environment]::NewLine
$remoteScript += "print('Deployed backup:',backup,flush=True)" + [Environment]::NewLine
$commandFile = New-TemporaryFile
$payloadFile = New-TemporaryFile
$remotePayload = '/tmp/ascent-deploy-' + [Guid]::NewGuid().ToString('N') + '.py'
try {
    [IO.File]::WriteAllText($payloadFile.FullName, $remoteScript, [Text.UTF8Encoding]::new($false))
    & pscp -batch -hostkey $PiHostKey -pw $PiPassword $payloadFile.FullName "streamer@${PiAddress}:$remotePayload"
    if ($LASTEXITCODE -ne 0) { throw 'Deployment script transfer failed' }
    [IO.File]::WriteAllText($commandFile.FullName, "sudo python3 $remotePayload", [Text.UTF8Encoding]::new($false))
    & plink -ssh -batch -hostkey $PiHostKey -pw $PiPassword -m $commandFile.FullName "streamer@$PiAddress"
    if ($LASTEXITCODE -ne 0) { throw 'Deployment failed' }
} finally {
    Remove-Item -LiteralPath $commandFile.FullName
    Remove-Item -LiteralPath $payloadFile.FullName
}
