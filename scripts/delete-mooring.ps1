# delete-mooring.ps1
#
# Permanently removes a mooring from the TSCTide database, bypassing the
# normal PIN-gated UI flow. Intended for admin use when:
#   - A user has lost their PIN and asked for the mooring to be cleared.
#   - Test moorings need to be removed without going through the UI.
#   - The UI itself is unreachable.
#
# Cascades into observations, calendar_events and pin_failed_attempts,
# matches the behaviour of the application's delete_mooring() helper, and
# also removes the on-disk feed file at /app/data/feeds/mooring_NNN.ics.
# Activity log entries are preserved and a synthetic "mooring_deleted"
# audit row is added so the history reflects what happened.
#
# Usage (from any PowerShell window in any directory):
#   .\delete-mooring.ps1 -MooringId 42
#   .\delete-mooring.ps1 -MooringId 42 -Container my-other-container-name
#   .\delete-mooring.ps1 -MooringId 42 -Force          # skip confirmation
#
# The script is interactive by default and asks for confirmation before
# touching the database. Pass -Force to skip the prompt (suitable for
# scripted use).

[CmdletBinding()]
param (
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 100)]
    [int]$MooringId,

    [string]$Container = "tidal-access",

    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# Sanity-check that docker is on PATH and the named container is running.
# Failing here saves a confused user a longer debugging session.
try {
    $running = docker ps --filter "name=^/$Container$" --format "{{.Names}}" 2>$null
} catch {
    Write-Error "Could not invoke 'docker'. Is Docker Desktop running and on PATH?"
    exit 1
}
if ($running -ne $Container) {
    Write-Error "Container '$Container' is not running. Use -Container <name> if it has a different name."
    exit 1
}

# Show what we are about to do and require explicit confirmation, unless
# -Force is given. Prevents accidental destruction from a typo.
$padded = '{0:D3}' -f $MooringId
$feedPath = "/app/data/feeds/mooring_$padded.ics"

Write-Host ""
Write-Host "About to delete mooring #$MooringId from container '$Container':" -ForegroundColor Yellow
Write-Host "  - Row in   moorings"
Write-Host "  - Rows in  observations         (any observations for this mooring)"
Write-Host "  - Rows in  calendar_events      (any stored access windows)"
Write-Host "  - Rows in  pin_failed_attempts  (any PIN lockout state)"
Write-Host "  - Feed file $feedPath           (if present)"
Write-Host ""
Write-Host "Activity-log entries are PRESERVED, plus a 'mooring_deleted' audit row is added." -ForegroundColor Yellow
Write-Host ""

if (-not $Force) {
    $reply = Read-Host "Type the mooring ID ($MooringId) to confirm"
    if ([string]::IsNullOrWhiteSpace($reply) -or [int]$reply -ne $MooringId) {
        Write-Host "Mooring ID did not match. Aborted." -ForegroundColor Red
        exit 2
    }
}

# Build the SQL as a here-string. Using a transaction means a single
# failure rolls back the entire deletion, leaving no half-deleted state.
# strftime('%Y-%m-%dT%H:%M:%SZ', 'now') matches the to_utc_str format
# the rest of the application uses.
$sql = @"
BEGIN TRANSACTION;
DELETE FROM observations         WHERE mooring_id = $MooringId;
DELETE FROM calendar_events      WHERE mooring_id = $MooringId;
DELETE FROM pin_failed_attempts  WHERE mooring_id = $MooringId;
DELETE FROM moorings             WHERE mooring_id = $MooringId;
INSERT INTO activity_log (timestamp, scope, mooring_id, severity, event_type, message)
VALUES (strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'mooring', $MooringId, 'warning',
        'mooring_deleted',
        'Mooring #$MooringId removed via delete-mooring.ps1 (admin action)');
COMMIT;
"@

Write-Host "Running SQL transaction..." -ForegroundColor Cyan

# Pipe the SQL into sqlite3 via docker exec. -i (no -t) for non-interactive
# stdin. ASCII-only here-string keeps Windows code-page issues out of scope.
$sql | docker exec -i $Container sqlite3 /app/data/tides.db
if ($LASTEXITCODE -ne 0) {
    Write-Error "SQL execution failed (exit code $LASTEXITCODE). Database state should be unchanged due to the transaction wrapper."
    exit 3
}

Write-Host "Database deletion complete." -ForegroundColor Green

# Now the feed file. This is best-effort - the database is the source of
# truth, the feed file is regenerated from it on every request, and an
# orphaned .ics for a now-deleted mooring is harmless because serve_feed
# returns 404 once the mooring row is gone.
Write-Host "Removing feed file (if present)..." -ForegroundColor Cyan
docker exec $Container sh -c "rm -f $feedPath"
if ($LASTEXITCODE -eq 0) {
    Write-Host "Feed file cleanup complete." -ForegroundColor Green
} else {
    Write-Warning "Feed file removal returned exit code $LASTEXITCODE. The database has been updated regardless; you may want to remove the file manually."
}

Write-Host ""
Write-Host "Mooring #$MooringId has been deleted." -ForegroundColor Green
Write-Host "Note: the System Activity log will show the 'mooring_deleted' audit entry." -ForegroundColor Gray
