param(
    [string]$PythonExecutable = "",
    [switch]$PublishRoot,
    [switch]$VerifyApprovedCandidate,
    [string]$ExpectedCandidateSha256 = "",
    [ValidateSet("on", "off")]
    [string]$Upx = "off",
    [ValidatePattern("^[0-9]{14}$")]
    [string]$BuildIdOverride = "",
    [ValidatePattern("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
    [string]$CandidateLabel = "default",
    [switch]$AllowDirtyCandidate,
    [string]$ExpectedSourceProvenanceSha256 = "",
    [string]$ReleaseBundlePath = "",
    [string]$ExpectedReleaseBundleSha256 = ""
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $PythonExecutable) {
    $projectPython = Join-Path $projectRoot ".venv-stability\Scripts\python.exe"
    $PythonExecutable = if (Test-Path -LiteralPath $projectPython) { $projectPython } else { "python" }
}
$specPath = Join-Path $projectRoot "packaging\cuoti_launcher.spec"
$exeName = -join @([char]0x9519, [char]0x9898, [char]0x672C, ".exe")
$distExe = Join-Path (Join-Path $projectRoot "dist") $exeName
$candidateDist = Join-Path (Join-Path (Join-Path $projectRoot "build") "candidates") $CandidateLabel
if ($CandidateLabel -ne "default") {
    New-Item -ItemType Directory -Path $candidateDist -Force | Out-Null
    $distExe = Join-Path $candidateDist $exeName
}
$rootExe = Join-Path $projectRoot $exeName
$buildId = if ($BuildIdOverride) { $BuildIdOverride } else { Get-Date -Format "yyyyMMddHHmmss" }
$previousExe = $null
$generatedStatic = Join-Path $projectRoot "build\generated-static"
$versionFile = Join-Path $projectRoot "build\cuoti-version.txt"
$mathJaxFontRelative = "cuoti_app\static\vendor\mathjax\es5\output\chtml\fonts\woff-v2"
$mathJaxFontSource = Join-Path $projectRoot $mathJaxFontRelative
$provenanceDir = Join-Path (Join-Path $projectRoot "build\provenance") $CandidateLabel
$provenanceManifest = Join-Path $provenanceDir "source-provenance.json"
$provenancePatch = Join-Path $provenanceDir "tracked-changes.patch"
$provenanceArchive = Join-Path $provenanceDir "source-snapshot.zip"

Push-Location $projectRoot
try {
    if ($PublishRoot -and $VerifyApprovedCandidate) {
        throw "PublishRoot and VerifyApprovedCandidate are mutually exclusive"
    }
    $gitStatus = @(git status --porcelain)
    if ($LASTEXITCODE -ne 0) {
        throw "Git working-tree check failed with exit code $LASTEXITCODE"
    }
    $isBuildMode = -not $PublishRoot -and -not $VerifyApprovedCandidate
    $dirtyCandidateAllowed = (
        $AllowDirtyCandidate -and $isBuildMode -and $CandidateLabel -ne "default"
    )
    if ($gitStatus.Count -ne 0 -and $isBuildMode -and -not $dirtyCandidateAllowed) {
        throw "Launcher builds require a clean working tree. Commit or remove pending changes first."
    }
    if ($PublishRoot -or $VerifyApprovedCandidate) {
        $ExpectedCandidateSha256 = $ExpectedCandidateSha256.Trim().ToUpperInvariant()
        if ($ExpectedCandidateSha256 -notmatch "^[0-9A-F]{64}$") {
            throw "PublishRoot requires -ExpectedCandidateSha256 for the approved candidate"
        }
        $ExpectedSourceProvenanceSha256 = $ExpectedSourceProvenanceSha256.Trim().ToUpperInvariant()
        if ($ExpectedSourceProvenanceSha256 -notmatch "^[0-9A-F]{64}$") {
            throw "Approved candidates require -ExpectedSourceProvenanceSha256"
        }
        if (-not (Test-Path -LiteralPath $provenanceManifest -PathType Leaf)) {
            throw "Approved candidate source provenance manifest does not exist"
        }
        if (-not (Test-Path -LiteralPath $provenanceArchive -PathType Leaf)) {
            throw "Approved candidate source snapshot does not exist"
        }
        $actualProvenanceSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $provenanceManifest).Hash
        if ($actualProvenanceSha256 -ne $ExpectedSourceProvenanceSha256) {
            throw "Approved candidate source provenance SHA-256 does not match"
        }
        if (-not (Test-Path -LiteralPath $distExe -PathType Leaf)) {
            throw "Approved candidate does not exist: $distExe"
        }
        $candidateHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $distExe).Hash
        if ($candidateHash -ne $ExpectedCandidateSha256) {
            throw "Approved candidate SHA-256 does not match dist executable"
        }
        $buildId = (Get-Item -LiteralPath $distExe).VersionInfo.ProductVersion
        if ($buildId -notmatch "^[0-9]{14}$") {
            throw "Approved candidate ProductVersion is not a 14-digit BUILD_ID"
        }
        if ($PublishRoot) {
            $ExpectedReleaseBundleSha256 = $ExpectedReleaseBundleSha256.Trim().ToUpperInvariant()
            if (-not $ReleaseBundlePath) {
                throw "PublishRoot requires -ReleaseBundlePath"
            }
            if ($ExpectedReleaseBundleSha256 -notmatch "^[0-9A-F]{64}$") {
                throw "PublishRoot requires -ExpectedReleaseBundleSha256"
            }
            if (-not (Test-Path -LiteralPath $ReleaseBundlePath -PathType Leaf)) {
                throw "Approved release bundle does not exist"
            }
            & $PythonExecutable tools\release_bundle.py verify `
                --root $projectRoot `
                --bundle $ReleaseBundlePath `
                --expected-sha256 $ExpectedReleaseBundleSha256 `
                --expected-candidate $distExe `
                --expected-provenance $provenanceManifest `
                --expected-product-version $buildId
            if ($LASTEXITCODE -ne 0) {
                throw "Approved release bundle verification failed with exit code $LASTEXITCODE"
            }
            $approvedBundle = Get-Content -LiteralPath $ReleaseBundlePath -Raw | ConvertFrom-Json
            $previousRelative = $approvedBundle.rollback.previous_executable.archive_path
            if (-not $previousRelative) {
                throw "Approved release bundle does not declare its release-scoped rollback path"
            }
            $previousExe = Join-Path $projectRoot $previousRelative
        }
    }
    else {
        New-Item -ItemType Directory -Path $provenanceDir -Force | Out-Null
        & $PythonExecutable tools\capture_source_provenance.py `
            --root $projectRoot `
            --manifest $provenanceManifest `
            --patch-output $provenancePatch `
            --archive-output $provenanceArchive
        if ($LASTEXITCODE -ne 0) {
            throw "Source provenance capture failed with exit code $LASTEXITCODE"
        }
        if (-not (Test-Path -LiteralPath $provenanceManifest -PathType Leaf)) {
            throw "Source provenance capture did not create the manifest"
        }
        if (-not (Test-Path -LiteralPath $provenanceArchive -PathType Leaf)) {
            throw "Source provenance capture did not create the source snapshot"
        }
        if ($gitStatus.Count -ne 0 -and (Get-Item -LiteralPath $provenancePatch).Length -eq 0) {
            $untrackedCount = @(git ls-files --others --exclude-standard).Count
            if ($untrackedCount -eq 0) {
                throw "Dirty candidate provenance is missing the tracked changes patch"
            }
        }
        $actualProvenanceSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $provenanceManifest).Hash
        npm run lint:js
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend lint failed with exit code $LASTEXITCODE"
        }
        & $PythonExecutable tools\generate_launcher_assets.py
        if ($LASTEXITCODE -ne 0) {
            throw "Launcher asset generation failed with exit code $LASTEXITCODE"
        }
        if (Test-Path -LiteralPath $generatedStatic) {
            Remove-Item -LiteralPath $generatedStatic -Recurse -Force
        }
        $mathJaxFonts = @(Get-ChildItem -LiteralPath $mathJaxFontSource -Filter "*.woff" -File -ErrorAction SilentlyContinue)
        if ($mathJaxFonts.Count -eq 0) {
            throw "MathJax CHTML font assets are missing: $mathJaxFontSource. Restore the .woff files before building."
        }
        Copy-Item -LiteralPath (Join-Path $projectRoot "cuoti_app\static") -Destination $generatedStatic -Recurse
        $stagedMathJaxFontSource = Join-Path `
            $generatedStatic `
            'vendor\mathjax\es5\output\chtml\fonts\woff-v2'
        $stagedMathJaxFonts = @(Get-ChildItem -LiteralPath $stagedMathJaxFontSource -Filter "*.woff" -File -ErrorAction SilentlyContinue)
        if ($stagedMathJaxFonts.Count -ne $mathJaxFonts.Count) {
            throw "MathJax CHTML font assets were not copied completely: source $($mathJaxFonts.Count), staged $($stagedMathJaxFonts.Count)."
        }
        $generatedSw = Join-Path $generatedStatic "sw.js"
        $swSource = Get-Content -LiteralPath $generatedSw -Raw
        if (-not $swSource.Contains("__CUOTI_BUILD_ID__")) {
            throw "Service worker BUILD_ID placeholder is missing"
        }
        $generatedSwSource = $swSource.Replace("__CUOTI_BUILD_ID__", $buildId)
        [System.IO.File]::WriteAllText(
            $generatedSw,
            $generatedSwSource,
            [System.Text.UTF8Encoding]::new($false)
        )
        & $PythonExecutable tools\generate_windows_version.py $buildId $versionFile
        if ($LASTEXITCODE -ne 0) {
            throw "Windows version metadata generation failed with exit code $LASTEXITCODE"
        }
        $env:CUOTI_VERSION_FILE = $versionFile
        $env:CUOTI_BUILD_STATIC = $generatedStatic
        $env:CUOTI_UPX = $Upx
        & $PythonExecutable -c "import customtkinter, PyInstaller, PIL, pymupdf, pystray"
        if ($LASTEXITCODE -ne 0) {
            throw "Launcher build dependency check failed with exit code $LASTEXITCODE"
        }
        $pyInstallerArgs = @("--noconfirm", "--clean")
        if ($CandidateLabel -ne "default") {
            $pyInstallerArgs += @("--distpath", $candidateDist)
        }
        $pyInstallerArgs += $specPath
        & $PythonExecutable -m PyInstaller @pyInstallerArgs
        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller failed with exit code $LASTEXITCODE"
        }
        if (-not (Test-Path -LiteralPath $distExe)) {
            throw "PyInstaller did not create the expected executable: $distExe"
        }
    }
    if ($VerifyApprovedCandidate) {
        Write-Output "BUILD_MODE=approved-candidate-verified"
        Write-Output "BUILD_ID=$buildId"
        Write-Output "SOURCE_PROVENANCE_MANIFEST=$provenanceManifest"
        Write-Output "SOURCE_PROVENANCE_SHA256=$actualProvenanceSha256"
        Get-Item -LiteralPath $distExe | Select-Object FullName, Length, LastWriteTime
        return
    }
    $publishedExe = $distExe
    $buildMode = "candidate-only"
    if ($PublishRoot) {
        $releaseOutput = @(
            & $PythonExecutable tools\release_bundle.py publish `
                --root $projectRoot `
                --bundle $ReleaseBundlePath `
                --expected-sha256 $ExpectedReleaseBundleSha256 `
                --expected-candidate $distExe `
                --expected-provenance $provenanceManifest `
                --expected-product-version $buildId
        )
        if ($LASTEXITCODE -ne 0) {
            throw "Approved release bundle publish failed with exit code $LASTEXITCODE"
        }
        $releaseResult = $releaseOutput[-1] | ConvertFrom-Json
        if ($releaseResult.status -eq "formal-published") {
            $publishedExe = $rootExe
            $buildMode = "formal-published"
        }
        elseif ($releaseResult.status -eq "pending-replacement") {
            $publishedExe = Join-Path $projectRoot $releaseResult.pending_path
            $buildMode = "pending-replacement"
            Write-Warning "The current executable is running; the verified release bundle is bound to one fixed pending replacement."
        }
        else {
            throw "Approved release bundle returned an unsupported publish status"
        }
    }
    Write-Output "BUILD_MODE=$buildMode"
    Write-Output "BUILD_ID=$buildId"
    Write-Output "SOURCE_PROVENANCE_MANIFEST=$provenanceManifest"
    Write-Output "SOURCE_PROVENANCE_SHA256=$actualProvenanceSha256"
    if ($PublishRoot) {
        Write-Output "ROLLBACK_PATH=$previousExe"
    }
    Get-Item -LiteralPath $publishedExe | Select-Object FullName, Length, LastWriteTime
}
finally {
    Remove-Item Env:CUOTI_BUILD_STATIC -ErrorAction SilentlyContinue
    Remove-Item Env:CUOTI_VERSION_FILE -ErrorAction SilentlyContinue
    Remove-Item Env:CUOTI_UPX -ErrorAction SilentlyContinue
    Pop-Location
}
