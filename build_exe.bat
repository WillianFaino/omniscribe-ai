@echo off
chcp 65001 >nul
title OmniScribe AI - Gerenciador de Build e Instalacao
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
echo [1/4] Instalando e checando dependencias de requirements.txt...
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

:: 3. Gerenciamento de Versoes Anteriores
set RESTORE_BACKUP=0
if not exist "dist" goto skip_clean

echo ===============================================================================
echo  VERSAO ANTERIOR DETECTADA NA PASTA 'dist\'
echo ===============================================================================
echo  Deseja limpar os arquivos da versao anterior antes de compilar?
echo    [1] Atualizar com seguranca (PRESERVA suas Atas, Gravacoes e .env) [PADRAO]
echo    [2] Limpeza Total (Zera a pasta dist e exclui atas antigas)
echo    [3] Continuar sem limpar nada
echo.
set CLEAN_MODE=1
set /p CLEAN_MODE="Digite 1, 2 ou 3 (Pressione ENTER para o padrao [1] Seguro): "

:: Fecha eventuais processos abertos para evitar travamento do arquivo
taskkill /f /im OmniScribe.exe >nul 2>&1
taskkill /f /im ZoomTranscriber.exe >nul 2>&1

if "%CLEAN_MODE%"=="2" goto clean_wipe
if "%CLEAN_MODE%"=="3" goto skip_clean

:clean_backup
echo.
echo [BACKUP] Salvando suas atas e configuracoes antes da limpeza...
if not exist "_temp_backup" mkdir "_temp_backup"

:: Salva arquivo de ambiente (.env)
if exist "dist\OmniScribe\.env" copy /Y "dist\OmniScribe\.env" "_temp_backup\.env" >nul 2>&1
if exist "dist\.env" copy /Y "dist\.env" "_temp_backup\.env" >nul 2>&1
if exist ".env" copy /Y ".env" "_temp_backup\.env" >nul 2>&1

:: Salva historico de atas e transcricoes
if exist "dist\OmniScribe\atas_reunioes" xcopy /E /I /Y "dist\OmniScribe\atas_reunioes" "_temp_backup\atas_reunioes" >nul 2>&1
if exist "dist\atas_reunioes" xcopy /E /I /Y "dist\atas_reunioes" "_temp_backup\atas_reunioes" >nul 2>&1
if exist "atas_reunioes" xcopy /E /I /Y "atas_reunioes" "_temp_backup\atas_reunioes" >nul 2>&1

echo [LIMPEZA] Removendo instalacoes e binarios antigos...
rmdir /s /q "dist" >nul 2>&1
rmdir /s /q "build" >nul 2>&1
del /f /q *.spec >nul 2>&1
echo [OK] Pasta limpa com seguranca.
set RESTORE_BACKUP=1
goto skip_clean

:clean_wipe
echo.
echo [LIMPEZA] Removendo completamente a pasta dist, build e atas antigas...
rmdir /s /q "dist" >nul 2>&1
rmdir /s /q "build" >nul 2>&1
del /f /q *.spec >nul 2>&1
echo [OK] Pasta dist zerada do zero.
goto skip_clean

:skip_clean
echo.
:: 4. Selecao do Formato de Compilacao
echo Escolha o formato de empacotamento:
echo   [1] Modo Turbo Instantaneo (--onedir)  - Abre em menos de 1 segundo! [RECOMENDADO]
echo   [2] Executavel Unico (--onefile)       - Arquivo unico portatil (~3 a 5s para abrir)
echo.
set BUILD_MODE=1
set /p BUILD_MODE="Digite 1 ou 2 (Pressione ENTER para o padrao [1] Turbo): "

if "%BUILD_MODE%"=="2" goto build_onefile
goto build_onedir

:build_onedir
echo.
echo [3/4] Empacotando aplicacao em Modo Turbo Instantaneo (--onedir --windowed)...
python -m PyInstaller --noconfirm --clean --onedir --windowed --name "OmniScribe" --paths "src" --collect-all "customtkinter" --collect-all "google.genai" src/main.py

if %errorlevel% neq 0 (
    echo.
    echo [ERRO] Ocorreu uma falha durante o empacotamento com o PyInstaller.
    pause
    exit /b 1
)

echo.
echo [4/4] Configurando arquivos e restaurando dados...
if not exist "dist\OmniScribe" mkdir "dist\OmniScribe"
if exist ".env.example" copy /Y ".env.example" "dist\OmniScribe\.env.example" >nul
if exist ".env" copy /Y ".env" "dist\OmniScribe\.env" >nul

if "%RESTORE_BACKUP%"=="1" (
    echo [RESTAURACAO] Injetando suas atas e .env salvos na nova versao...
    if exist "_temp_backup\.env" copy /Y "_temp_backup\.env" "dist\OmniScribe\.env" >nul
    if exist "_temp_backup\atas_reunioes" xcopy /E /I /Y "_temp_backup\atas_reunioes" "dist\OmniScribe\atas_reunioes" >nul
    rmdir /s /q "_temp_backup" >nul 2>&1
    echo [OK] Atas anteriores restauradas com sucesso!
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
goto end

:build_onefile
echo.
echo [3/4] Empacotando aplicacao via PyInstaller (--onefile --windowed)...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "OmniScribe" --paths "src" --collect-all "customtkinter" --collect-all "google.genai" src/main.py

if %errorlevel% neq 0 (
    echo.
    echo [ERRO] Ocorreu uma falha durante o empacotamento com o PyInstaller.
    pause
    exit /b 1
)

echo.
echo [4/4] Configurando arquivos e restaurando dados...
if not exist "dist" mkdir "dist"
if exist ".env.example" copy /Y ".env.example" "dist\.env.example" >nul
if exist ".env" copy /Y ".env" "dist\.env" >nul

if "%RESTORE_BACKUP%"=="1" (
    echo [RESTAURACAO] Injetando suas atas e .env salvos na nova versao...
    if exist "_temp_backup\.env" copy /Y "_temp_backup\.env" "dist\.env" >nul
    if exist "_temp_backup\atas_reunioes" xcopy /E /I /Y "_temp_backup\atas_reunioes" "dist\atas_reunioes" >nul
    rmdir /s /q "_temp_backup" >nul 2>&1
    echo [OK] Atas anteriores restauradas com sucesso!
)

echo.
echo ===============================================================================
echo                          BUILD CONCLUIDO COM SUCESSO!
echo ===============================================================================
echo  Executavel portavel gerado em:
echo    ^> dist\OmniScribe.exe
echo ===============================================================================
goto end

:end
echo.
pause
