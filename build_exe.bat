@echo off
chcp 65001 >nul
title OmniScribe AI - Build Executavel
echo ===============================================================================
echo            OmniScribe AI - Universal Meeting Transcriber & Minute Generator
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

:: 3. Selecao do Formato de Compilacao
echo Escolha o modo de empacotamento:
echo   [1] Modo Turbo Instantaneo (--onedir)  - Abre em menos de 1 segundo! [RECOMENDADO]
echo   [2] Executavel Unico (--onefile)       - Arquivo unico portatil (~3 a 5s para abrir)
echo.
set "BUILD_MODE=1"
set /p "BUILD_MODE=Digite 1 ou 2 (Pressione ENTER para o padrao [1] Turbo): "

if "%BUILD_MODE%"=="2" (
    echo.
    echo [2/3] Empacotando aplicacao via PyInstaller (--onefile --windowed)...
    python -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name "OmniScribe" ^
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

    echo.
    echo [3/3] Copiando arquivos de configuracao para a pasta dist/...
    if not exist "dist" mkdir "dist"
    if exist ".env.example" (
        copy /Y ".env.example" "dist\.env.example" >nul
    )
    if exist ".env" (
        copy /Y ".env" "dist\.env" >nul
    )

    echo.
    echo ===============================================================================
    echo                          BUILD CONCLUIDO COM SUCESSO!
    echo ===============================================================================
    echo  Executavel portavel gerado em:
    echo    ^> dist\OmniScribe.exe
    echo ===============================================================================
) else (
    echo.
    echo [2/3] Empacotando aplicacao em Modo Turbo Instantaneo (--onedir --windowed)...
    python -m PyInstaller --noconfirm --clean --onedir --windowed ^
        --name "OmniScribe" ^
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

    echo.
    echo [3/3] Copiando arquivos de configuracao para dist\OmniScribe\...
    if exist ".env.example" (
        copy /Y ".env.example" "dist\OmniScribe\.env.example" >nul
    )
    if exist ".env" (
        copy /Y ".env" "dist\OmniScribe\.env" >nul
    )

    echo.
    echo ===============================================================================
    echo                     BUILD TURBO CONCLUIDO COM SUCESSO!
    echo ===============================================================================
    echo  Executavel com inicializacao ultra-rapida gerado em:
    echo    ^> dist\OmniScribe\OmniScribe.exe
    echo.
    echo  DICA: Voce pode clicar com o botao direito em 'OmniScribe.exe' e
    echo        selecionar 'Enviar para' -^> 'Area de trabalho (criar atalho)'!
    echo ===============================================================================
)

echo.
pause
