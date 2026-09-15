[CmdletBinding()]
param(
    [switch]$PullModels,
    [switch]$WithHosted,
    [switch]$HostedOnly,
    [string]$DotenvPath = "",
    [string]$StateRoot = ""
)

$ErrorActionPreference = "Stop"
$OllamaVersionExpected = "0.34.0"
$EmbeddingModel = "bge-m3"
$AnswerModel = "gemma4:e2b"
$FinalExitCode = 0
if ($HostedOnly) { $WithHosted = $true }

function Fail([string]$Message) {
    throw $Message
}

function Test-TcpPort([string]$HostName, [int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($HostName, $Port)
        return $task.Wait(2000) -and $client.Connected
    }
    finally {
        $client.Dispose()
    }
}

function Invoke-WithEnvironment([hashtable]$Variables, [scriptblock]$Action) {
    $previous = @{}
    foreach ($name in $Variables.Keys) {
        $previous[$name] = [System.Environment]::GetEnvironmentVariable($name, "Process")
        [System.Environment]::SetEnvironmentVariable($name, [string]$Variables[$name], "Process")
    }
    try {
        & $Action
    }
    finally {
        foreach ($name in $Variables.Keys) {
            [System.Environment]::SetEnvironmentVariable($name, $previous[$name], "Process")
        }
    }
}

$RepoRoot = (& git rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $RepoRoot) { Fail "Run this script from a Git checkout." }
$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot.Trim())
if ([System.IO.Path]::GetFullPath($PSScriptRoot) -ne $RepoRoot) {
    Fail "Keep p4-evaluate.ps1 at the repository root."
}
Set-Location -LiteralPath $RepoRoot

if (-not (Test-Path -LiteralPath "src/graphmind/evaluation.py" -PathType Leaf)) {
    Fail "This checkout does not contain P4. Commit and pull the P4 candidate first."
}
if (-not (Test-Path -LiteralPath "tests/fixtures/evaluation/metakgp_50.json" -PathType Leaf) -or
    -not (Test-Path -LiteralPath "Crawler/cleaned_wiki.jsonl" -PathType Leaf)) {
    Fail "The frozen P4 fixture or source snapshot is missing."
}
$dirty = & git status --porcelain --untracked-files=all
if ($LASTEXITCODE -ne 0 -or $dirty) { Fail "Test an exact committed candidate from a clean checkout." }

$GitHash = (& git rev-parse HEAD).Trim()
$GitShort = (& git rev-parse --short=12 HEAD).Trim()
if (-not $StateRoot) {
    $StateRoot = Join-Path $env:LOCALAPPDATA "GraphMind\p4-evaluation"
}
if (-not $DotenvPath) {
    $DotenvPath = Join-Path $RepoRoot ".env"
}
$DotenvPath = [System.IO.Path]::GetFullPath($DotenvPath)
if (-not (Test-Path -LiteralPath $DotenvPath -PathType Leaf)) {
    Fail "Private dotenv not found: $DotenvPath"
}
if (-not (Test-TcpPort "127.0.0.1" 7687)) {
    Fail "Neo4j is not listening on 127.0.0.1:7687."
}

$RunId = "{0}-{1}-{2}" -f ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")), $GitShort, $PID
$RunRoot = Join-Path $StateRoot "runs\$RunId"
$CandidateRoot = Join-Path $RunRoot "candidate"
$Venv = Join-Path $RunRoot "venv"
$Reports = Join-Path $RunRoot "reports"
$LogFile = Join-Path $RunRoot "p4-evaluate.log"
$ResultsFile = Join-Path $RunRoot "results.json"
New-Item -ItemType Directory -Force -Path $CandidateRoot, $Reports | Out-Null
Start-Transcript -LiteralPath $LogFile -Force | Out-Null

try {
    Write-Host "GraphMind P4 native Windows evaluation"
    Write-Host "Commit: $GitHash"
    Write-Host "Run directory: $RunRoot"

    $Archive = Join-Path $RunRoot "candidate.zip"
    & git archive --format=zip -o $Archive $GitHash
    if ($LASTEXITCODE -ne 0) { Fail "Could not export the committed candidate." }
    Expand-Archive -LiteralPath $Archive -DestinationPath $CandidateRoot

    $Ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $Ollama) { Fail "Ollama is missing. Install version $OllamaVersionExpected first." }
    $OllamaVersion = (& ollama --version 2>&1 | Out-String).Trim()
    if ($OllamaVersion -notmatch [regex]::Escape($OllamaVersionExpected)) {
        Fail "Expected Ollama $OllamaVersionExpected; found: $OllamaVersion"
    }
    if (-not (Test-TcpPort "127.0.0.1" 11434)) {
        Fail "Ollama is not listening on 127.0.0.1:11434. Start Ollama and rerun."
    }
    if ($PullModels) {
        & ollama pull $EmbeddingModel
        if ($LASTEXITCODE -ne 0) { Fail "Could not pull $EmbeddingModel." }
        if (-not $HostedOnly) {
            & ollama pull $AnswerModel
            if ($LASTEXITCODE -ne 0) { Fail "Could not pull $AnswerModel." }
        }
    }

    $Tags = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 10
    $Names = @($Tags.models | ForEach-Object { $_.name })
    $RequiredModels = @($EmbeddingModel)
    if (-not $HostedOnly) { $RequiredModels += $AnswerModel }
    foreach ($required in $RequiredModels) {
        if ($Names -notcontains $required -and $Names -notcontains "${required}:latest") {
            Fail "Missing local model $required. Rerun with -PullModels."
        }
    }
    $RequiredModelNames = @($RequiredModels | ForEach-Object { $_; "${_}:latest" })
    $SelectedModels = @($Tags.models | Where-Object {
        $_.name -in $RequiredModelNames
    } | Select-Object name, digest, size, details)
    $SelectedModels | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $RunRoot "ollama-models.json") -Encoding utf8

    $Computer = Get-CimInstance Win32_ComputerSystem
    $OperatingSystem = Get-CimInstance Win32_OperatingSystem
    $Environment = [ordered]@{
        timestamp_utc = [DateTime]::UtcNow.ToString("o")
        git_commit = $GitHash
        os = $OperatingSystem.Caption
        os_version = $OperatingSystem.Version
        architecture = $env:PROCESSOR_ARCHITECTURE
        logical_processors = $Computer.NumberOfLogicalProcessors
        memory_bytes = [int64]$Computer.TotalPhysicalMemory
        python = (& python --version 2>&1 | Out-String).Trim()
        ollama_cli = $OllamaVersion
        ollama_server = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:11434/api/version"
        models = $SelectedModels
    }
    $Environment | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath (Join-Path $RunRoot "environment.json") -Encoding utf8

    & python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the evaluation virtual environment." }
    $Python = Join-Path $Venv "Scripts\python.exe"
    $GraphMind = Join-Path $Venv "Scripts\graphmind.exe"
    & $Python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { Fail "Could not update pip." }
    & $Python -m pip install $CandidateRoot
    if ($LASTEXITCODE -ne 0) { Fail "Could not install the committed candidate." }
    & $Python -m pip check
    if ($LASTEXITCODE -ne 0) { Fail "Installed dependency check failed." }

    Push-Location $CandidateRoot
    try {
        & $Python -B -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { Fail "Offline regression tests failed." }
        if (-not $HostedOnly) {
            Invoke-WithEnvironment @{
                GRAPHMIND_RUN_LOCAL_MODEL_INTEGRATION = "1"
                GRAPHMIND_LOCAL_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
                GRAPHMIND_LOCAL_EMBEDDING_MODEL = $EmbeddingModel
                GRAPHMIND_LOCAL_ANSWER_MODEL = $AnswerModel
                PYTHONDONTWRITEBYTECODE = "1"
                PYTHONPATH = ""
            } {
                & $Python -B -m unittest discover -s tests/integration -p test_real_stores.py -v
                if ($LASTEXITCODE -ne 0) { Fail "Real local model contract failed." }
            }
        }
    }
    finally {
        Pop-Location
    }

    $Results = [System.Collections.Generic.List[object]]::new()
    function Invoke-Evaluation(
        [string]$Label,
        [ValidateSet("chunk", "document")][string]$GraphScope,
        [bool]$VectorOnly,
        [ValidateSet("local", "hosted")][string]$AnswerMode
    ) {
        $WorkDir = Join-Path $RunRoot "work-$Label"
        $Report = Join-Path $Reports "$Label.json"
        $Collection = "gm_p4_{0}_{1}_{2}" -f $GitShort, ($Label -replace '[^A-Za-z0-9]', '_'), $PID
        $Variables = @{
            GRAPHMIND_COLLECTION_NAME = $Collection
            GRAPHMIND_EMBEDDING_PROVIDER = "ollama"
            GRAPHMIND_EMBEDDING_MODEL = $EmbeddingModel
            GRAPHMIND_EMBEDDING_MODEL_ID = "BAAI/bge-m3"
            GRAPHMIND_EMBEDDING_REVISION = "auto"
            GRAPHMIND_EMBEDDING_PRECISION = "auto"
            GRAPHMIND_EMBEDDING_BASE_URL = "http://127.0.0.1:11434"
            GRAPHMIND_EMBEDDING_DIMENSION = "1024"
            GRAPHMIND_RETRIEVAL_GRAPH_SCOPE = $GraphScope
            GRAPHMIND_ANSWER_MODE = $AnswerMode
        }
        if ($AnswerMode -eq "local") {
            $Variables.GRAPHMIND_ANSWER_PROVIDER = "ollama"
            $Variables.GRAPHMIND_ANSWER_MODEL = $AnswerModel
            $Variables.OLLAMA_BASE_URL = "http://127.0.0.1:11434"
            $Variables.OLLAMA_API_KEY = ""
        }
        $Arguments = @(
            "--dotenv", $DotenvPath, "evaluate",
            "--fixture", (Join-Path $CandidateRoot "tests/fixtures/evaluation/metakgp_50.json"),
            "--source", (Join-Path $CandidateRoot "Crawler/cleaned_wiki.jsonl"),
            "--work-dir", $WorkDir,
            "--report", $Report,
            "--label", $Label,
            "--graph-scope", $GraphScope,
            "--resolve-model-identity"
        )
        if ($VectorOnly) { $Arguments += "--vector-only" }
        $ExitCode = 0
        Invoke-WithEnvironment $Variables {
            & $GraphMind @Arguments
            $script:EvaluationExitCode = $LASTEXITCODE
        }
        $ExitCode = $script:EvaluationExitCode
        if ($ExitCode -notin @(0, 4)) { Fail "$Label failed before producing a quality result (exit $ExitCode)." }
        if (-not (Test-Path -LiteralPath $Report -PathType Leaf)) { Fail "$Label did not write its report." }
        $Results.Add([ordered]@{ label = $Label; exit_code = $ExitCode; report = $Report })
    }

    if (-not $HostedOnly) {
        Invoke-Evaluation "local-chunk" "chunk" $false "local"
        Invoke-Evaluation "local-vector-only" "chunk" $true "local"
        Invoke-Evaluation "local-document" "document" $false "local"
    }
    if ($WithHosted) {
        $ValidationCode = @'
from pathlib import Path
import sys
from graphmind.config import Settings
settings = Settings.load(
    dotenv_path=Path(sys.argv[1]),
    environ={"GRAPHMIND_ANSWER_MODE": "hosted"},
)
settings.validate(require_provider_secret=True)
print("Hosted provider configuration is present; secret value was not printed.")
'@
        & $Python -B -c $ValidationCode $DotenvPath
        if ($LASTEXITCODE -ne 0) { Fail "Hosted provider configuration is invalid." }
        Invoke-Evaluation "hosted-chunk" "chunk" $false "hosted"
    }

    $Results | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ResultsFile -Encoding utf8
    $ResultByLabel = @{}
    foreach ($item in $Results) {
        $ResultByLabel[$item.label] = $item
        $Report = Get-Content -LiteralPath $item.report -Raw | ConvertFrom-Json
        $Summary = $Report.summary
        $ExpectedExitCode = if ([bool]$Summary.gate_passed) { 0 } else { 4 }
        if ($item.exit_code -ne $ExpectedExitCode) {
            Fail ("{0} exit/report mismatch: exit={1}, gate_passed={2}" -f
                $item.label, $item.exit_code, $Summary.gate_passed)
        }
        Write-Host ("{0}: gate={1} answers={2}/{3} retrieval={4}/{5} graph_target_recall={6:N3} p95_ms={7:N1}" -f
            $item.label, $Summary.gate_passed, $Summary.answer_passes, $Summary.total_cases,
            $Summary.retrieval_passes, $Summary.retrieval_case_count,
            $Summary.graph_target_recall, $Summary.p95_duration_ms)
    }

    $finalDirty = & git -C $RepoRoot status --porcelain --untracked-files=all
    if ($LASTEXITCODE -ne 0 -or $finalDirty) { Fail "The P4 harness unexpectedly changed the checkout." }
    Write-Host "P4 evaluation completed. Reports and environment evidence: $RunRoot"
    if ($HostedOnly) {
        if ($ResultByLabel["hosted-chunk"].exit_code -eq 4) {
            Write-Host "P4 gate is not passed: the hosted chunk-scope baseline missed its threshold."
            $FinalExitCode = 4
        }
        else {
            Write-Host "Hosted automated threshold passed; combine with the prior local report and perform manual factual-support review."
        }
    }
    elseif ($ResultByLabel["local-chunk"].exit_code -eq 4) {
        Write-Host "P4 gate is not passed: the accepted local chunk-scope baseline missed its threshold."
        $FinalExitCode = 4
    }
    elseif (-not $WithHosted) {
        Write-Host "P4 remains incomplete until the hosted baseline is run."
    }
    elseif ($ResultByLabel["hosted-chunk"].exit_code -eq 4) {
        Write-Host "P4 gate is not passed: the hosted chunk-scope baseline missed its threshold."
        $FinalExitCode = 4
    }
    else {
        Write-Host "Automated local and hosted thresholds passed. Manual factual-support review is still required."
    }
}
finally {
    Stop-Transcript | Out-Null
}
if ($FinalExitCode -ne 0) { exit $FinalExitCode }
