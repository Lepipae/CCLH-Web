# Script de PowerShell equivalente a start.sh para Windows

$ErrorActionPreference = "Stop"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "   Iniciando Cartas Contra la Humanidad" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# 1. Compilar Frontend (React)
Write-Host "[1/3] Compilando frontend (React)..." -ForegroundColor Yellow
$frontendPath = Join-Path $PSScriptRoot "frontend"
try {
    Push-Location $frontendPath
    npm run build
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Error compilando el frontend." -ForegroundColor Red
        exit 1
    }
}
catch {
    Write-Host "Error durante la compilación del frontend: $_" -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}

# 2. Detectar comando de Python
$pythonCmd = if (Get-Command python3 -ErrorAction SilentlyContinue) { "python3" }
             elseif (Get-Command python -ErrorAction SilentlyContinue) { "python" }
             elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" }
             else { $null }

if (-not $pythonCmd) {
    Write-Host "[!] No se encontró Python instalado en el sistema (python, python3 o py)." -ForegroundColor Red
    exit 1
}

# 3. Detectar ejecutable de ngrok
$ngrokCmd = if (Get-Command ngrok -ErrorAction SilentlyContinue) { "ngrok" }
            elseif (Test-Path "$PSScriptRoot\backend\ngrok.exe") { "$PSScriptRoot\backend\ngrok.exe" }
            elseif (Test-Path "$PSScriptRoot\ngrok.exe") { "$PSScriptRoot\ngrok.exe" }
            else { $null }

if (-not $ngrokCmd) {
    Write-Host "[!] No se encontró ngrok. Por favor, asegúrate de tenerlo instalado en el PATH o en la carpeta del proyecto." -ForegroundColor Red
    exit 1
}

# 4. Iniciando backend (Python)
Write-Host "[2/3] Iniciando backend (Python)..." -ForegroundColor Yellow
$backendPath = Join-Path $PSScriptRoot "backend"
$appProcess = Start-Process -FilePath $pythonCmd -ArgumentList "app.py" -WorkingDirectory $backendPath -PassThru -NoNewWindow

# 5. Iniciando ngrok para acceso público
Write-Host "[3/3] Iniciando ngrok para acceso público..." -ForegroundColor Yellow
$ngrokLog = Join-Path $PSScriptRoot "ngrok.log"
$ngrokProcess = Start-Process -FilePath $ngrokCmd -ArgumentList "http 3000 --log=stdout" -RedirectStandardOutput $ngrokLog -PassThru -WindowStyle Hidden

try {
    Write-Host "Esperando enlace de ngrok..." -ForegroundColor Gray
    $url = $null
    for ($i = 1; $i -le 10; $i++) {
        Start-Sleep -Seconds 1
        try {
            $tunnels = Invoke-RestMethod -Uri "http://127.0.0.1:4040/api/tunnels" -ErrorAction SilentlyContinue
            if ($tunnels -and $tunnels.tunnels) {
                $secureTunnel = $tunnels.tunnels | Where-Object { $_.public_url -like "https://*" } | Select-Object -First 1
                if ($secureTunnel) {
                    $url = $secureTunnel.public_url
                    break
                }
            }
        } catch {
            # Ignorar mientras ngrok se termina de iniciar
        }
    }

    Write-Host "==================================================================" -ForegroundColor Cyan
    if ($url) {
        Write-Host "✅ ¡TODO LISTO! TU URL PÚBLICA ES:" -ForegroundColor Green
        Write-Host "👉  $url  👈" -ForegroundColor Green
        Write-Host "Comparte este enlace para que otros se unan." -ForegroundColor Green
    } else {
        Write-Host "⚠️ No se pudo obtener la URL de ngrok. Verifica ngrok.log." -ForegroundColor Yellow
    }
    Write-Host "==================================================================" -ForegroundColor Cyan
    Write-Host "Manteniendo el servidor abierto... (Presiona Ctrl+C para cerrar)" -ForegroundColor Gray

    # Mantener el script activo mientras los procesos estén ejecutándose
    while (-not $ngrokProcess.HasExited -and -not $appProcess.HasExited) {
        Start-Sleep -Seconds 1
    }
}
finally {
    Write-Host ""
    Write-Host "Cerrando servidores..." -ForegroundColor Yellow
    if ($appProcess -and -not $appProcess.HasExited) {
        Stop-Process -Id $appProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if ($ngrokProcess -and -not $ngrokProcess.HasExited) {
        Stop-Process -Id $ngrokProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
