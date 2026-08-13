<#
Package the scanner as a Lambda ZIP instead of a container image.

    ./build-zip.ps1              build and report the artifact
    ./build-zip.ps1 -Deploy      also upload it to $ZipFuncName

Why this exists
---------------
The container path cannot ship from this machine. Its base image
(public.ecr.aws/lambda/python:3.12) carries a 433 MB rootfs layer, and pushing
that single blob failed roughly thirty times on 3 Aug -- always at the TCP write
to Docker Desktop's internal gateway, always on that one digest, while every
smaller blob in the same run landed fine. Not credentials (login succeeds, small
layers push with the same token), not ECR, not connectivity (the same base image
PULLS fine). One long-lived multi-minute upload over Wi-Fi is simply fragile.

A ZIP never uploads a base layer at all -- AWS supplies the runtime -- so the
payload drops from 433 MB to roughly 50 MB.

The Dockerfile's opening line says the image exists because "trafilatura,
psycopg2 and the Anthropic SDK together exceed Lambda's 250 MB unzipped limit".
Measured on the built image, site-packages is 185 MB. That premise no longer
holds, so the image is buying nothing this path does not.
#>
param(
    [switch]$Deploy,
    [string]$ZipFuncName = 'spx-scanner-zip'
)

. (Join-Path $PSScriptRoot 'common.ps1')

$root  = Split-Path $PSScriptRoot -Parent
$build = Join-Path $root 'build'
$pkg   = Join-Path $build 'pkg'
$zip   = Join-Path $build 'spx-scanner.zip'

# Source that ships. Mirrors the Dockerfile's COPY list exactly -- tests, docs,
# eval fixtures and the venv are all deliberately absent.
$srcDirs  = @('alerts', 'analysis', 'collectors', 'db', 'market', 'utils')
$srcFiles = @('config.py', 'main.py', 'lambda_handler.py')

Write-Step 'Resolving version'
# utils/version.py reads APP_VERSION when present and falls back to git. A ZIP
# has no .git either, so the version is computed here and set as a Lambda
# environment variable, exactly as deploy.ps1 bakes it into the image.
$count = (git -C $root rev-list --count HEAD).Trim()
$hash  = (git -C $root rev-parse --short=7 HEAD).Trim()
$dirty = if ((git -C $root status --porcelain)) { '-dirty' } else { '' }
$version = "r$count-g$hash$dirty"
Write-Ok $version
if ($dirty) { Write-Note 'working tree is dirty -- this build is not reproducible' }

Write-Step 'Clearing the build directory'
if (Test-Path $build) { Remove-Item -Recurse -Force $build }
New-Item -ItemType Directory -Force $pkg | Out-Null

Write-Step 'Installing dependencies for Linux'
# --platform + --only-binary is the whole trick. A plain `pip install --target`
# on Windows silently resolves Windows wheels; psycopg2-binary and lxml carry
# compiled extensions, so those import-fail on Lambda at the first cycle rather
# than at build time. Pinning the platform makes that failure impossible.
pip install `
    --platform manylinux2014_x86_64 `
    --implementation cp `
    --python-version 3.12 `
    --only-binary=:all: `
    --no-cache-dir `
    --target $pkg `
    -r (Join-Path $root 'requirements.txt') 2>&1 | Select-Object -Last 3
if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }

Write-Step 'Trimming what Lambda does not need'
# Saves ~15-20 MB with no runtime effect: bytecode is regenerated on cold start,
# and tests/headers inside site-packages are never imported.
foreach ($p in @('__pycache__', '*.dist-info/RECORD', 'pip', 'setuptools', 'wheel')) {
    Get-ChildItem -Path $pkg -Filter $p -Recurse -Force -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Step 'Adding application source'
foreach ($d in $srcDirs) {
    Copy-Item -Recurse -Force (Join-Path $root $d) (Join-Path $pkg $d)
    Get-ChildItem -Path (Join-Path $pkg $d) -Filter '__pycache__' -Recurse -Force `
        -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
foreach ($f in $srcFiles) { Copy-Item -Force (Join-Path $root $f) $pkg }
Write-Ok "$($srcDirs.Count) packages + $($srcFiles.Count) modules"

Write-Step 'Verifying the payload is Linux-native'
# One wrong wheel is a cold-start ImportError in production, so check rather
# than trust: every compiled extension must be an ELF .so, and no .pyd (the
# Windows extension suffix) may survive anywhere in the tree.
$pyd = Get-ChildItem -Path $pkg -Filter '*.pyd' -Recurse -Force -ErrorAction SilentlyContinue
if ($pyd) { throw "Windows extensions present -- the platform pin did not apply: $($pyd[0].Name)" }
$so = Get-ChildItem -Path $pkg -Filter '*.so' -Recurse -Force -ErrorAction SilentlyContinue
if (-not $so) { throw 'no compiled extensions found at all -- psycopg2/lxml are missing.' }
foreach ($critical in @('psycopg2', 'lxml')) {
    if (-not (Test-Path (Join-Path $pkg $critical))) { throw "$critical is not in the package." }
}
Write-Ok "$($so.Count) Linux .so files, no .pyd, psycopg2 + lxml present"

Write-Step 'Compressing'
Compress-Archive -Path (Join-Path $pkg '*') -DestinationPath $zip -CompressionLevel Optimal -Force
$unzipMb = [math]::Round((Get-ChildItem $pkg -Recurse -Force | Measure-Object Length -Sum).Sum / 1MB, 1)
$zipMb   = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Ok "$zipMb MB zipped / $unzipMb MB unzipped"
Write-Note 'Lambda limits: 250 MB unzipped, 50 MB for a direct upload (larger needs S3)'
if ($unzipMb -gt 250) { throw "unzipped $unzipMb MB exceeds Lambda's 250 MB limit." }
if ($zipMb -gt 50) { Write-Note 'over 50 MB -- deployment will route through S3' }

if (-not $Deploy) {
    Write-Host "`nBuilt $zip" -ForegroundColor Cyan
    Write-Host "Re-run with -Deploy to upload it to $ZipFuncName." -ForegroundColor DarkGray
    return
}

Write-Step "Uploading to $ZipFuncName"
if (-not (Test-LambdaExists $ZipFuncName)) {
    throw "$ZipFuncName does not exist yet. Create it first -- this script updates code, it does not provision."
}

Write-Step 'Confirming concurrency is pinned before touching production code'
# A live schedule can already be firing against $ZipFuncName even if it was
# never routed through schedule.ps1's guard -- e.g. scheduled before that
# guard existed, or enabled by hand. Checked here too, not only in
# schedule.ps1, so a code deploy can never land on an unpinned production
# function between cycles.
Assert-ZipConcurrencyPinned
Write-Ok 'confirmed: reserved concurrency = 1'

# No --publish: that needs lambda:PublishVersion, which the deployer policy does
# not grant, and nothing here uses versions or aliases. $LATEST is the target.
aws lambda update-function-code --function-name $ZipFuncName --zip-file "fileb://$zip" `
    --region $Region | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'update-function-code failed.' }
aws lambda wait function-updated --function-name $ZipFuncName --region $Region
if ($LASTEXITCODE -ne 0) {
    throw "function-updated wait failed or timed out for $ZipFuncName -- the new code may not be active. Not stamping APP_VERSION until this is confirmed."
}

# APP_VERSION must be MERGED into the existing environment, never assigned.
# --environment replaces the whole map, so writing just APP_VERSION would delete
# the other ~75 variables -- database password, API keys, every tuned threshold
# -- and the next cycle would start with defaults against no database.
Write-Step 'Merging APP_VERSION into the existing environment'
$existing = aws lambda get-function-configuration --function-name $ZipFuncName `
    --region $Region --query 'Environment.Variables' --output json | ConvertFrom-Json
$map = @{}
if ($existing) { $existing.PSObject.Properties | ForEach-Object { $map[$_.Name] = $_.Value } }
$before = $map.Count
$map['APP_VERSION'] = $version
if ($before -eq 0) {
    throw "refusing to write: $ZipFuncName reports no environment variables, so this would not be a merge. Run sync-env.ps1 first."
}
$json = @{ Variables = $map } | ConvertTo-Json -Depth 3 -Compress
$tmp = Join-Path $env:TEMP "spx-env-$([guid]::NewGuid().ToString('N')).json"
Set-Content -Path $tmp -Value $json -Encoding utf8 -NoNewline
try {
    aws lambda update-function-configuration --function-name $ZipFuncName --region $Region `
        --environment "file://$tmp" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'update-function-configuration failed.' }
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}
Write-Ok "$ZipFuncName is running $version ($($map.Count) env vars preserved)"
