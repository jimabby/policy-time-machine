<#
.SYNOPSIS
    Run the whole Policy Time Machine engine, end to end, with one command.

.DESCRIPTION
    The Makefile is this project's task runner, and `make` is not installed on a
    default Windows box - so this is the same tour in the shell that is. No
    Airflow, no API key, no network: PTM_OFFLINE=1 puts a deterministic rule
    evaluator where the model goes, which is how CI runs it and how the demo is
    rehearsed.

    The order is the order the project's argument builds in, not alphabetical.
    Each step says what question it answers before it answers it, because a wall
    of output nobody can read is the failure this project is about.

    Some steps exit non-zero on purpose. `ptm.gate expenses v2` FAILS on the
    shipped fixture - two rulings are reversed - and that is the correct answer,
    not a broken install: policy v1 reverses the same two, so the proposal
    introduces neither. Steps like that are marked "expected" below and do not
    fail the run. Anything else returning non-zero does.

.PARAMETER Domain
    Which domain to run against. Defaults to expenses; refunds is the other one.

.PARAMETER Version
    The candidate policy version. Defaults to v2.

.PARAMETER Setup
    Create .venv and install requirements-dev.txt first. Needed once, unless a
    virtualenv is already here.

.PARAMETER Step
    Pause after each section, so it can be talked over.

.PARAMETER Quick
    Skip the slow ones (selftest, the joint grid) - about four seconds instead
    of twenty.

.EXAMPLE
    .\demo.ps1

.EXAMPLE
    .\demo.ps1 -Setup           # first run on a machine with no virtualenv

.EXAMPLE
    .\demo.ps1 -Domain refunds -Step
#>
[CmdletBinding()]
param(
    [string]$Domain = "expenses",
    [string]$Version = "v2",
    [switch]$Setup,
    [switch]$Step,
    [switch]$Quick
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# --------------------------------------------------------------- the interpreter
# .venv is what the Makefile builds; .venv-af is the heavier one with Airflow in
# it, which works just as well for the engine. Either will do, and neither is
# checked in, so a fresh clone needs -Setup once.
function Resolve-Python {
    foreach ($candidate in @(".venv\Scripts\python.exe", ".venv-af\Scripts\python.exe")) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

if ($Setup) {
    Write-Host "creating .venv and installing requirements-dev.txt ..." -ForegroundColor Cyan
    python -m venv .venv
    .\.venv\Scripts\python -m pip install --quiet --upgrade pip
    .\.venv\Scripts\python -m pip install --quiet -r requirements-dev.txt
    Write-Host "done." -ForegroundColor Cyan
    Write-Host ""
}

$Python = Resolve-Python
if (-not $Python) {
    Write-Host "No virtualenv found." -ForegroundColor Red
    Write-Host "Run:  .\demo.ps1 -Setup" -ForegroundColor Red
    Write-Host "(it creates .venv and installs pydantic, PyYAML, pytest and ruff - no Airflow)"
    exit 2
}

# ------------------------------------------------------------------ the fixtures
# Relative, like the Makefile's. Without these ptm.config falls back to
# /opt/airflow/include, which only exists inside the container - the symptom is
# "no domain config 'expenses'" and an empty list of available domains.
$env:PTM_INCLUDE_DIR = "./include"
$env:PTM_DB = "./include/ptm.db"
$env:PTM_OFFLINE = "1"

$script:Results = @()

function Invoke-Section {
    param(
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)][string]$Question,
        [Parameter(Mandatory)][string[]]$Arguments,
        # Non-zero is the answer here, not a failure. See the note at the top.
        [switch]$ExpectNonZero,
        [switch]$SkipWhenQuick
    )

    if ($Quick -and $SkipWhenQuick) {
        Write-Host ""
        Write-Host "-- $Title (skipped: -Quick)" -ForegroundColor DarkGray
        return
    }

    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor DarkGray
    Write-Host "  $Title" -ForegroundColor Cyan
    Write-Host "  $Question" -ForegroundColor DarkGray
    Write-Host ("  $ python -m " + ($Arguments -join " ")) -ForegroundColor DarkGray
    Write-Host ("=" * 78) -ForegroundColor DarkGray

    $started = Get-Date
    & $Python -m @Arguments
    $code = $LASTEXITCODE
    $elapsed = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)

    $ok = ($code -eq 0)
    if ($ExpectNonZero) { $ok = $true }

    $script:Results += [pscustomobject]@{
        Step = $Title; Exit = $code; Seconds = $elapsed; Ok = $ok
    }

    if (-not $ok) {
        Write-Host ""
        Write-Host "  ^ $Title exited $code, which it should not have." -ForegroundColor Red
    }

    if ($Step) {
        Write-Host ""
        Read-Host "  [enter] for the next step"
    }
}

Write-Host ""
Write-Host "Policy Time Machine - the engine, end to end" -ForegroundColor White
Write-Host "  interpreter : $Python"
Write-Host "  domain      : $Domain / $Version"
Write-Host "  judge       : offline (deterministic rules; no API key, no network)"

# 1. Is the configuration honest about the policies it claims to implement?
Invoke-Section -Title "Lint" `
    -Question "Do the offline rules still implement the policies they cite?" `
    -Arguments @("ptm.lint")

# 2. Is the policy text itself usable before anything is spent on it?
Invoke-Section -Title "Preflight" `
    -Question "What is wrong with the policy before a replay is paid for?" `
    -Arguments @("ptm.preflight", $Domain)

# 3. The whole loop, in one command. This is the demo.
Invoke-Section -Title "Selftest" `
    -Question "The whole loop: replay, attribution, blast radius, confirmation, gate, proposal." `
    -Arguments @("ptm.selftest", $Domain, $Version) -SkipWhenQuick

# 4. Why this is not a hand-rolled backtest.
Invoke-Section -Title "Point-in-time check" `
    -Question "How many cases does a naive backtest get wrong? (all in one direction)" `
    -Arguments @("ptm.pit_check", $Domain, $Version)

# 5. The point of the project. Non-zero is the correct answer on this fixture.
Invoke-Section -Title "Precedent gate" `
    -Question "Does this policy reverse a ruling a human already made?" `
    -Arguments @("ptm.gate", $Domain, $Version) -ExpectNonZero

Invoke-Section -Title "Precedent gate (introduced only)" `
    -Question "...and how many of those reversals is the PROPOSAL responsible for?" `
    -Arguments @("ptm.gate", $Domain, $Version, "--introduced-only")

# 6. Attribution stops at "clause 1.1 moves 48 decisions". This is the step after.
Invoke-Section -Title "Threshold sweep" `
    -Question "So what should the number actually be?" `
    -Arguments @("ptm.sweep", $Domain, $Version, "1.1", "amount_gbp", "25,50,75,100,150,250")

Invoke-Section -Title "Threshold sweep (a dial that decides nothing)" `
    -Question "A flat curve is not an insensitive threshold. Watch for the WARNING." `
    -Arguments @("ptm.sweep", $Domain, $Version, "6.1", "grade", "2,3,4,6")

Invoke-Section -Title "Joint sweep" `
    -Question "Two dials at once - one curve cannot show them interacting." `
    -Arguments @("ptm.sweep", $Domain, $Version, "--joint",
                 "1.1:amount_gbp=25,50,75,100,150", "3.1:days_notice=3,7,14,21") `
    -SkipWhenQuick

# 7. The band that comes before the backfill rather than after it.
Invoke-Section -Title "Sample size" `
    -Question "How big a change could this much history even detect?" `
    -Arguments @("ptm.report", $Domain, $Version, "--power", "--target", "0.20")

# 8. What the numbers above are worth.
Invoke-Section -Title "Rules vs the judge" `
    -Question "Do the offline rules agree with the judge the sweep rests on?" `
    -Arguments @("ptm.rules", $Domain, $Version)

Invoke-Section -Title "Calibration" `
    -Question "Is the judge RIGHT? Scored against the humans who ruled." `
    -Arguments @("ptm.calibration", $Domain, $Version)

# 9. The one place a model writes rather than judges. Writes nothing without --write.
Invoke-Section -Title "Proposal" `
    -Question "Draft the next version of the policy from the evidence (writes nothing)." `
    -Arguments @("ptm.proposal", $Domain, $Version)

# 10. The artefacts built to leave the room.
Invoke-Section -Title "Export" `
    -Question "Every panel with its caveats attached, as one file." `
    -Arguments @("ptm.report", $Domain, $Version, "-o", "ptm-$Domain-$Version.json")

Invoke-Section -Title "Precedents" `
    -Question "The only output that cannot be recomputed, in a form you can move." `
    -Arguments @("ptm.precedents", $Domain, "-o", "ptm-$Domain-precedents.json")

# 11. What has stopped earning its disk. Counting only.
Invoke-Section -Title "Retention (dry run)" `
    -Question "Which rows have stopped earning their disk?" `
    -Arguments @("ptm.prune", "--dry-run")

# ------------------------------------------------------------------- the summary
Write-Host ""
Write-Host ("=" * 78) -ForegroundColor DarkGray
Write-Host "  Summary" -ForegroundColor White
Write-Host ("=" * 78) -ForegroundColor DarkGray
$script:Results | ForEach-Object {
    $mark = "ok  "
    $colour = "Green"
    if (-not $_.Ok) { $mark = "FAIL"; $colour = "Red" }
    if ($_.Ok -and $_.Exit -ne 0) { $mark = "ok* "; $colour = "Yellow" }
    Write-Host ("  {0} {1,-44} exit {2}  {3,5}s" -f $mark, $_.Step, $_.Exit, $_.Seconds) `
        -ForegroundColor $colour
}
Write-Host "  ok* = exited non-zero, which is the correct answer for that step" -ForegroundColor DarkGray

$failed = @($script:Results | Where-Object { -not $_.Ok })
Write-Host ""
if ($failed.Count -gt 0) {
    Write-Host "$($failed.Count) step(s) failed." -ForegroundColor Red
    exit 1
}

Write-Host "Everything ran. Wrote ptm-$Domain-$Version.json and ptm-$Domain-precedents.json." -ForegroundColor Green
Write-Host ""
Write-Host "Next, the half that needs Airflow - the DAGs, the backfill that is the" -ForegroundColor White
Write-Host "simulation engine, the human-in-the-loop queue and the Diff Explorer:" -ForegroundColor White
Write-Host ""
Write-Host "  1. Start Docker Desktop"
Write-Host "  2. docker compose up --build -d"
Write-Host "  3. http://localhost:8080/ptm/   (the Diff Explorer; no login)"
Write-Host "  4. docker compose exec airflow airflow backfill create --dag-id replay_$Domain ``"
Write-Host "       --from-date 2024-09-01 --to-date 2026-09-01 --run-backwards"
Write-Host ""
exit 0
