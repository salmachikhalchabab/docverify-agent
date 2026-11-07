# Start the demo website in this window. Keep the window open; closing it stops the website.
#
#   powershell -ExecutionPolicy Bypass -File start_website.ps1                       # guarded_human on port 8000
#   powershell -ExecutionPolicy Bypass -File start_website.ps1 -Variant prompt_only_human -Port 8001
#   powershell -ExecutionPolicy Bypass -File start_website.ps1 -Mock                 # small registry (Nour, Layla)
#
# Versions (keys): baseline, prompt_only, guarded, prompt_only_human, guarded_human
#   *_human = every action (email, registration, notice) waits for a staff member
# Students: http://127.0.0.1:<port>/     Staff: http://127.0.0.1:<port>/staff
param(
    [switch]$Mock,
    [string]$Variant = 'guarded_human',
    [int]$Port = 8000
)

Set-Location $PSScriptRoot
$Host.UI.RawUI.WindowTitle = "docverify website $Variant :$Port - keep open"

if ($Mock) {
    $env:REFERENCE_BACKEND = 'mock_sqlite'
    $env:REFERENCE_DB_PATH = 'data/mock_registry.sqlite'
} else {
    $env:REFERENCE_BACKEND = 'p1_sqlite'
    $env:REFERENCE_DB_PATH = 'data/p1/registry_agent.sqlite'
}
$env:AGENT_VARIANT = $Variant
$env:PORT = "$Port"
$env:PYTHONUNBUFFERED = '1'
# $env:LLM_MODEL = 'gemini/gemini-3.5-flash-lite'   # faster; uncomment if its daily quota is not used up

Write-Host "registry: $($env:REFERENCE_BACKEND)   version: $Variant"
Write-Host "students: http://127.0.0.1:$Port/     staff: http://127.0.0.1:$Port/staff"
# everything printed below is also saved to runs\website_<port>.log (to find out why it stopped, if it does)
& .venv\Scripts\python.exe -X utf8 ui\app.py 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath "runs\website_$Port.log" -Append
Write-Host ""
Write-Host "The website has stopped (exit code $LASTEXITCODE). See runs\website_$Port.log." -ForegroundColor Yellow
