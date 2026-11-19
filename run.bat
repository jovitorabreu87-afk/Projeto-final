@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "VENV_DIR=%CD%\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

echo.
echo ==============================================
echo          RADAR CULTURAL - INICIADOR
echo ==============================================
echo.

if not exist "%VENV_PY%" (
    echo [1/4] Criando ambiente virtual Python...
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv "%VENV_DIR%"
        if errorlevel 1 goto :venv_error
    ) else (
        where python >nul 2>&1
        if errorlevel 1 goto :python_missing
        python -m venv "%VENV_DIR%"
        if errorlevel 1 goto :venv_error
    )
) else (
    echo [1/4] Ambiente virtual encontrado.
)

if not exist "%VENV_PY%" goto :venv_error

"%VENV_PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto :python_version_error

echo.
echo [2/4] Atualizando o instalador de pacotes...
"%VENV_PY%" -m ensurepip --upgrade
if errorlevel 1 goto :pip_error
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :pip_error

echo.
echo [3/4] Instalando as dependencias do Radar...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :requirements_error

if not exist ".env" (
    echo.
    echo Arquivo .env ainda nao existe. Criando a partir do modelo...
    if not exist ".env.example" goto :env_example_missing
    copy /Y ".env.example" ".env" >nul
    if errorlevel 1 goto :env_error
    echo.
    echo Preencha as chaves das APIs no Bloco de Notas, salve e feche.
    start "" notepad.exe "%CD%\.env"
    pause
)

echo.
echo [4/4] Iniciando o Radar Cultural...
echo.
echo Abra: http://127.0.0.1:5000/
echo Diagnostico: http://127.0.0.1:5000/api/health
echo Mantenha esta janela aberta enquanto usar o site.
echo Para encerrar, pressione Ctrl+C.
echo.
set "FLASK_DEBUG=0"
"%VENV_PY%" app.py
if errorlevel 1 goto :server_error
goto :end

:python_missing
echo ERRO: Python nao foi encontrado.
echo Instale Python 3.10 ou superior e marque a opcao "Add Python to PATH".
goto :fail

:python_version_error
echo ERRO: o ambiente virtual nao esta usando Python 3.10 ou superior.
echo Apague a pasta .venv e execute este arquivo novamente usando uma versao atual do Python.
goto :fail

:venv_error
echo ERRO ao criar o ambiente virtual. Verifique a instalacao do Python.
goto :fail

:pip_error
echo ERRO ao preparar o pip. Confira sua conexao com a internet e tente novamente.
goto :fail

:requirements_error
echo ERRO ao instalar requirements.txt. Confira a mensagem acima e sua conexao com a internet.
goto :fail

:env_example_missing
echo ERRO: .env.example nao foi encontrado. Extraia todos os arquivos do ZIP para a mesma pasta.
goto :fail

:env_error
echo ERRO ao criar o arquivo .env.
goto :fail

:server_error
echo O servidor foi encerrado com erro. Confira a mensagem acima.
goto :fail

:fail
echo.
pause
:end
endlocal
