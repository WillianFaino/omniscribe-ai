@echo off
chcp 65001 >nul
title Escutaí - Gerador do Instalador Grafico (Setup Wizard)
echo ===============================================================================
echo              Escutai - Gerador do Assistente de Instalacao
echo ===============================================================================
echo.

:: 1. Verificacao do Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] O interpretador Python nao foi detectado no PATH do Windows.
    echo Certifique-se de instalar o Python 3.11+ e marcar a opcao 'Add python.exe to PATH'.
    echo.
    pause
    exit /b 1
)

echo [OK] Python detectado:
python --version
echo.

:: 2. Dependencias
echo [1/3] Verificando dependencias necessarias...
python -m pip install -r requirements.txt >nul 2>&1
echo [OK] Dependencias prontas.
echo.

:: 3. Verificacao do binario Turbo (dist\Escutai\Escutai.exe)
if exist "dist\Escutai\Escutai.exe" goto build_setup

echo [AVISO] Os binarios do Escutai nao foram encontrados em 'dist\Escutai\'.
echo Compilando versao Turbo enxuta antes de empacotar o Instalador...
echo.
python -m PyInstaller --noconfirm --clean --onedir --windowed --name "Escutai" --paths "src" --collect-all "customtkinter" --collect-all "sherpa_onnx" --add-data "assets/mascot;assets/mascot" --hidden-import "google.genai" --exclude-module tzdata --exclude-module pytest --exclude-module unittest --exclude-module pandas --exclude-module scipy --exclude-module lxml --exclude-module openpyxl --exclude-module selenium --exclude-module SQLAlchemy --exclude-module psycopg2 src/main.py
if %errorlevel% neq 0 (
    echo [ERRO] Falha ao compilar os arquivos base do Escutai.
    pause
    exit /b 1
)
if exist ".env.example" copy /Y ".env.example" "dist\Escutai\.env.example" >nul
if exist ".env" copy /Y ".env" "dist\Escutai\.env" >nul

:build_setup
echo.
echo [2/3] Preparando modelos locais ONNX (Voiceprints + Whisper small INT8)...
python src/offline_asr_service.py --prepare small
if %errorlevel% neq 0 (
    echo [AVISO] Falha ao baixar modelos offline agora; o app fara o download sob demanda no primeiro uso.
)
if exist "models\voiceprint" (
    if not exist "dist\Escutai\models\voiceprint" mkdir "dist\Escutai\models\voiceprint"
    xcopy /E /I /Y "models\voiceprint" "dist\Escutai\models\voiceprint" >nul
)
if exist "models\asr\sherpa-onnx-whisper-small" (
    if not exist "dist\Escutai\models\asr\sherpa-onnx-whisper-small" mkdir "dist\Escutai\models\asr\sherpa-onnx-whisper-small"
    xcopy /E /I /Y "models\asr\sherpa-onnx-whisper-small" "dist\Escutai\models\asr\sherpa-onnx-whisper-small" >nul
)

echo.
echo [3/3] Gerando pacote compacto e compilando Instalador Nativo Instantaneo...
echo       (Stub Win32/.NET ~32 KB com Payload EOF -> Abertura em menos de 0.1s!)
echo.

:: Fecha instalador antigo se estiver em execucao
taskkill /f /im Escutai_Setup.exe >nul 2>&1
taskkill /f /im OmniScribe_Setup.exe >nul 2>&1

python make_package.py "dist\Escutai" "dist\app_data.zip"
if %errorlevel% neq 0 (
    echo [ERRO] Falha ao gerar o instalador nativo.
    pause
    exit /b 1
)

echo.
echo ===============================================================================
echo                   INSTALADOR GERADO COM SUCESSO!
echo ===============================================================================
echo  O instalador grafico profissional e autonomo foi gerado em:
echo    ^> dist\Escutai_Setup.exe
echo.
echo  COMO USAR:
echo    1. Basta dar dois cliques em 'Escutai_Setup.exe' em qualquer PC Windows!
echo    2. Ele possui interface moderna, permite escolher a pasta de destino,
echo       cria atalhos na Area de Trabalho e Menu Iniciar, e preserva suas atas.
echo ===============================================================================
echo.
pause

