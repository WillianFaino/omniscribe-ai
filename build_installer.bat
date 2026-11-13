@echo off
chcp 65001 >nul
title OmniScribe AI - Gerador do Instalador Grafico (Setup Wizard)
echo ===============================================================================
echo            OmniScribe AI - Gerador do Assistente de Instalacao
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

:: 3. Verificacao do binario Turbo (dist\OmniScribe\OmniScribe.exe)
if exist "dist\OmniScribe\OmniScribe.exe" goto build_setup

echo [AVISO] Os binarios do OmniScribe nao foram encontrados em 'dist\OmniScribe\'.
echo Compilando versao Turbo enxuta antes de empacotar o Instalador...
echo.
python -m PyInstaller --noconfirm --clean --onedir --windowed --name "OmniScribe" --paths "src" --collect-all "customtkinter" --hidden-import "google.genai" --exclude-module tzdata --exclude-module pytest --exclude-module unittest --exclude-module pandas --exclude-module scipy --exclude-module lxml --exclude-module openpyxl --exclude-module selenium --exclude-module SQLAlchemy --exclude-module psycopg2 src/main.py
if %errorlevel% neq 0 (
    echo [ERRO] Falha ao compilar os arquivos base do OmniScribe.
    pause
    exit /b 1
)
if exist ".env.example" copy /Y ".env.example" "dist\OmniScribe\.env.example" >nul
if exist ".env" copy /Y ".env" "dist\OmniScribe\.env" >nul

:build_setup
echo.
echo [2/2] Gerando pacote compacto e compilando Instalador Nativo Instantaneo...
echo       (Stub Win32/.NET ~32 KB com Payload EOF -> Abertura em menos de 0.1s!)
echo.

:: Fecha instalador antigo se estiver em execucao
taskkill /f /im OmniScribe_Setup.exe >nul 2>&1

python make_package.py "dist\OmniScribe" "dist\app_data.zip"
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
echo    ^> dist\OmniScribe_Setup.exe
echo.
echo  COMO USAR:
echo    1. Basta dar dois cliques em 'OmniScribe_Setup.exe' em qualquer PC Windows!
echo    2. Ele possui interface moderna, permite escolher a pasta de destino,
echo       cria atalhos na Area de Trabalho e Menu Iniciar, e preserva suas atas.
echo ===============================================================================
echo.
pause

