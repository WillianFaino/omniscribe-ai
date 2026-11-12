@echo off
chcp 65001 >nul
title Zoom Meeting Transcriber - Build Executavel
echo ===============================================================================
echo      Zoom Meeting Transcriber ^& Minute Generator - Script de Build
echo ===============================================================================
echo.

:: 1. Verificacao do Python no PATH do Windows
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

:: 2. Instalacao / Atualizacao das Dependencias
echo [1/3] Instalando e checando dependencias de requirements.txt...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERRO] Falha ao instalar as dependencias via pip.
    echo Verifique sua conexao com a internet ou permissoes do Python.
    echo.
    pause
    exit /b 1
)
echo [OK] Dependencias instaladas com sucesso.
echo.

:: 3. Compilacao com PyInstaller
echo [2/3] Empacotando aplicacao via PyInstaller (--onefile --windowed)...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "ZoomTranscriber" ^
    --paths "src" ^
    --collect-all "customtkinter" ^
    --collect-all "google.genai" ^
    src/main.py

if %errorlevel% neq 0 (
    echo.
    echo [ERRO] Ocorreu uma falha durante o empacotamento com o PyInstaller.
    pause
    exit /b 1
)

:: 4. Copia do modelo .env.example para a pasta dist
echo.
echo [3/3] Copiando .env.example para a pasta dist/...
if not exist "dist" mkdir "dist"
if exist ".env.example" (
    copy /Y ".env.example" "dist\.env.example" >nul
)

echo.
echo ===============================================================================
echo                          BUILD CONCLUIDO COM SUCESSO!
echo ===============================================================================
echo  Executavel gerado:
echo    ^> dist\ZoomTranscriber.exe
echo.
echo  COMO USAR:
echo  1. Na pasta 'dist\', copie o arquivo '.env.example' e renomeie para '.env'.
echo  2. Abra o arquivo '.env' no Bloco de Notas e insira sua GEMINI_API_KEY.
echo     (Ou simplesmente execute o 'ZoomTranscriber.exe' e cole sua chave na interface!)
echo  3. As atas executivas e transcricoes serao salvas automaticamente em:
echo     dist\atas_reunioes\AAAA\MM\
echo ===============================================================================
echo.
pause
