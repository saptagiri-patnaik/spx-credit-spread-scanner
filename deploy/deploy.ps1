<#
Build the image, push it to ECR, and point the Lambda at it.

This is the repeatable one -- run it after every code change. Safe to run any
number of times. Run provision.ps1 first, once, to create the function itself.
#>
param(
    # Skip the Docker build and just re-point Lambda at the image already in ECR.
    [switch]$NoBuild
)

. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Tooling
$account = Get-AwsAccount
$image = Get-ImageUri $account
$registry = Get-Registry $account
Write-Note "account $account / region $Region"

Write-Step 'Ensuring the ECR repository exists'
aws ecr describe-repositories --repository-names $RepoName --region $Region *> $null
if ($LASTEXITCODE -ne 0) {
    aws ecr create-repository --repository-name $RepoName --region $Region | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the ECR repository.' }
    Write-Ok "created $RepoName"
} else {
    Write-Ok "$RepoName already exists"
}

Write-Step 'Logging Docker in to ECR'
# This token lasts 12 hours, which is why login lives here rather than in a
# one-time script: a push that fails with "denied" is almost always this.
$password = aws ecr get-login-password --region $Region
if ($LASTEXITCODE -ne 0) { throw 'Could not get an ECR password.' }
$password | docker login --username AWS --password-stdin $registry
if ($LASTEXITCODE -ne 0) { throw 'docker login failed.' }

if (-not $NoBuild) {
    Write-Step 'Building the image (3-10 min the first time, cached after)'
    # --platform linux/amd64 is mandatory. Lambda runs x86; an ARM image built on
    # an ARM host fails at runtime with "exec format error", which reads like a
    # code bug rather than an architecture mismatch.
    #
    # --provenance=false --sbom=false are equally mandatory, for a much less
    # obvious reason. Buildx defaults to attaching a provenance attestation, which
    # forces the push into an OCI image *index* (manifest list) holding the real
    # image plus an attestation manifest with platform unknown/unknown. Lambda
    # accepts a single image manifest only and rejects the index with "the image
    # manifest, config or layer media type ... is not supported" -- an error that
    # names nothing you control and sounds like a broken base image.
    docker build --platform linux/amd64 --provenance=false --sbom=false `
        -t "$($RepoName):latest" $ProjectRoot
    if ($LASTEXITCODE -ne 0) { throw 'docker build failed.' }
    Write-Ok 'built'
}

Write-Step 'Pushing to ECR'
docker tag "$($RepoName):latest" "$($image):latest"
docker push "$($image):latest"
if ($LASTEXITCODE -ne 0) { throw 'docker push failed. If it says "denied", re-run this script (ECR login expires).' }
Write-Ok 'pushed'

Write-Step 'Verifying the manifest is Lambda-compatible'
# Checked here rather than left to create-function, because Lambda's rejection
# message does not mention manifests lists at all and sends you hunting the wrong bug.
$mediaType = aws ecr describe-images --repository-name $RepoName --region $Region `
    --image-ids imageTag=latest --query 'imageDetails[0].imageManifestMediaType' --output text
if ($LASTEXITCODE -ne 0) { throw 'Could not read the pushed manifest back from ECR.' }
if ($mediaType -match 'index|list') {
    throw "ECR holds a manifest $mediaType. Lambda needs a single image manifest. " +
          'Confirm the build used --provenance=false --sbom=false.'
}
Write-Ok $mediaType

if (-not (Test-LambdaExists)) {
    Write-Note "Function '$FuncName' does not exist yet."
    Write-Note 'The image is in ECR. Run deploy/provision.ps1 to create the function.'
    return
}

Write-Step 'Updating the function code'
aws lambda update-function-code --function-name $FuncName --region $Region `
    --image-uri "$($image):latest" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'update-function-code failed.' }

# Without this wait, a following configuration change races the code update and
# fails with ResourceConflictException.
aws lambda wait function-updated --function-name $FuncName --region $Region
Write-Ok 'Lambda is running the new image'

Write-Host "`nDeployed. Invoke it with the 'Lambda: Invoke now' task, or watch logs with 'Lambda: Tail logs'." -ForegroundColor Cyan
