# Native Windows entry; forward all backend options without evaluating .env.
$ErrorActionPreference = 'Stop'
$backendArguments = $args
$backendRoot = $PSScriptRoot
$pythonCommand = $env:CROWDSIM_PYTHON
if (-not $pythonCommand -and -not $env:CROWDSIM_CONDA) {
    $configPath = Join-Path $backendRoot '.env'
    if (Test-Path -LiteralPath $configPath) {
        foreach ($line in Get-Content -LiteralPath $configPath -Encoding UTF8) {
            if ($line -match '^\s*CROWDSIM_PYTHON\s*=(.*)$') {
                $pythonCommand = $Matches[1].Trim()
                if ($pythonCommand.Length -ge 2 -and
                    (($pythonCommand.StartsWith('"') -and $pythonCommand.EndsWith('"')) -or
                     ($pythonCommand.StartsWith("'") -and $pythonCommand.EndsWith("'")))) {
                    $pythonCommand = $pythonCommand.Substring(1, $pythonCommand.Length - 2)
                }
                break
            }
        }
    }
}
$previousPythonPath = $env:PYTHONPATH
$previousPythonUtf8 = $env:PYTHONUTF8
$env:PYTHONPATH = $backendRoot + $(if ($env:PYTHONPATH) { [IO.Path]::PathSeparator + $env:PYTHONPATH } else { '' })
if (-not $env:PYTHONUTF8) { $env:PYTHONUTF8 = '1' }
Push-Location -LiteralPath $backendRoot
try {
    if ($pythonCommand) {
        & $pythonCommand (Join-Path $backendRoot 'start_backend.py') @backendArguments
    } elseif ($env:CROWDSIM_CONDA) {
        $condaEnvironment = if ($env:CROWDSIM_CONDA_ENV) { $env:CROWDSIM_CONDA_ENV } else { 'sumo' }
        & $env:CROWDSIM_CONDA run --no-capture-output -n $condaEnvironment python (Join-Path $backendRoot 'start_backend.py') @backendArguments
    } else {
        $venvPython = Join-Path $backendRoot '.venv-backend/Scripts/python.exe'
        if (Test-Path -LiteralPath $venvPython) {
            & $venvPython (Join-Path $backendRoot 'start_backend.py') @backendArguments
        } else {
            & python (Join-Path $backendRoot 'start_backend.py') @backendArguments
        }
    }
    $backendExitCode = $LASTEXITCODE
} finally {
    Pop-Location
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONUTF8 = $previousPythonUtf8
}
exit $backendExitCode
