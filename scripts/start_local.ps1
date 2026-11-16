# Arranca o agente em local (Windows): API + worker + agenda + bot do Telegram, cada um na sua janela.
# Uso, na pasta do projeto:  powershell -ExecutionPolicy Bypass -File scripts\start_local.ps1
# Precisa do Redis em localhost:6379 e do .env preenchido.

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "Falta o .venv: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"; exit 1 }

$services = @(
    @{ Title = "API (painel em http://127.0.0.1:8001)"; Args = "-m uvicorn app.main:app --host 127.0.0.1 --port 8001" },
    @{ Title = "Worker Celery";                          Args = "-m celery -A app.tasks worker --pool=solo --loglevel=info" },
    @{ Title = "Agenda Celery (beat)";                    Args = "-m celery -A app.tasks beat --loglevel=info" },
    @{ Title = "Bot Telegram (polling)";                  Args = "scripts\telegram_poll.py" }
)

foreach ($s in $services) {
    $cmd = "`$host.UI.RawUI.WindowTitle = '$($s.Title)'; Set-Location '$root'; & '$py' $($s.Args)"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
}

Write-Host "Arrancado. Painel: http://127.0.0.1:8001  (fecha as 4 janelas para parar)"
