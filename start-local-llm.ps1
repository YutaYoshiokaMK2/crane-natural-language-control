$ErrorActionPreference = 'Stop'
$runtime = Join-Path $PSScriptRoot '.tmp/ollama'
$ollama = Join-Path $runtime 'ollama.exe'
if (-not (Test-Path -LiteralPath $ollama)) { throw 'Download and extract the official Ollama Windows ZIP to .tmp/ollama first.' }
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_MODELS = Join-Path $PSScriptRoot '.tmp/models'
$env:OLLAMA_NO_CLOUD = '1'
New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot 'artifacts') | Out-Null
Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'artifacts/ollama.stdout.log') -RedirectStandardError (Join-Path $PSScriptRoot 'artifacts/ollama.stderr.log')
Write-Host 'Ollama started on localhost:11434. Then run .tmp/ollama/ollama.exe pull qwen3:4b if not already downloaded.'
