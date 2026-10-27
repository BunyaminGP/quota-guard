# agy-run PowerShell function wrapper for agy-quota-guard
function agy-run {
    $pluginRoot = if ($env:AGY_QUOTA_GUARD_HOME) {
        $env:AGY_QUOTA_GUARD_HOME
    } else {
        Join-Path $HOME ".agents\plugins\agy-quota-guard"
    }
    $runner = Join-Path $pluginRoot "scripts\agy_runner.py"
    if (-not (Test-Path $runner)) {
        $localRunner = Join-Path $PSScriptRoot "..\scripts\agy_runner.py"
        if (Test-Path $localRunner) {
            $runner = (Resolve-Path $localRunner).Path
        } else {
            Write-Error "[agy-quota-guard] Runner not found at $runner"
            return 127
        }
    }
    if ($MyInvocation.ExpectingInput) {
        $inputText = $input | Out-String
        $inputText | python $runner @args
    } else {
        python $runner @args
    }
}
