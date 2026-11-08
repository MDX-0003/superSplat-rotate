# Build the minimum inputs for verify.mjs:
#   - src/index.ts (migration package: core + import glue)
#   - src/anim/spline.ts (editor CubicSpline, used as the reference implementation)
#
# Run from the repository root:
#   pwsh -File migration/supersplat-viewer-json-timeline/test/build.ps1

$ErrorActionPreference = 'Stop'

$testDir = $PSScriptRoot
$packageDir = Split-Path -Parent $testDir
$repoRoot = Resolve-Path (Join-Path $testDir '..\..\..')
$outDir = Join-Path $testDir '.build'

New-Item -ItemType Directory -Force -Path $outDir | Out-Null

Push-Location $repoRoot
try {
    npx tsc (Join-Path $packageDir 'src\index.ts') `
        --ignoreConfig `
        --outDir $outDir `
        --target es2022 `
        --module esnext `
        --strict `
        --skipLibCheck `
        --moduleResolution bundler
    if ($LASTEXITCODE -ne 0) {
        throw "src/index.ts compile failed (exit $LASTEXITCODE)"
    }

    npx tsc (Join-Path $repoRoot 'src\anim\spline.ts') `
        --ignoreConfig `
        --outDir $outDir `
        --target es2022 `
        --module esnext `
        --strict `
        --skipLibCheck `
        --moduleResolution bundler
    if ($LASTEXITCODE -ne 0) {
        throw "src/anim/spline.ts compile failed (exit $LASTEXITCODE)"
    }

    # Make Node treat .build as ESM and avoid the reparse warning.
    Set-Content -Path (Join-Path $outDir 'package.json') -Value '{ "type": "module" }' -Encoding utf8

    Write-Host "Build complete: $outDir"
} finally {
    Pop-Location
}
