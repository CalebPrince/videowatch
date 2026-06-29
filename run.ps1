param(
    [string]$Host = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$Reload
)

Set-Location -Path $PSScriptRoot

Write-Output "Installing Python requirements..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

Write-Output "Installing Playwright browsers..."
python -m playwright install || Write-Output "Playwright install failed"

$env:HOST = $Host
$env:PORT = $Port.ToString()
$env:RELOAD = if ($Reload) { 'true' } else { 'false' }

Write-Output "Starting server on $env:HOST:$env:PORT (reload=$env:RELOAD)"
python server.py
