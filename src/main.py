"""Ponto de entrada principal do Zoom Meeting Transcriber & Minute Generator.

Configura ambiente de execução, caminhos de importação, logging global e inicia a interface CustomTkinter.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Garante que o diretório 'src' esteja presente no sys.path para imports limpos
current_dir = Path(__file__).resolve().parent
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

from gemini_service import get_base_dir, load_api_key_from_env
from gui import run_gui


def setup_logging():
    """Configura o sistema de logging da aplicação para console e arquivo rotativo."""
    base_dir = get_base_dir()
    logs_dir = base_dir / "atas_reunioes" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    log_file = logs_dir / "app.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_file), encoding="utf-8", mode="a"),
        ],
    )


def main():
    """Inicializa o sistema e dispara o loop de eventos da interface gráfica."""
    setup_logging()
    logger = logging.getLogger("zoom_transcriber.main")
    logger.info("Iniciando Zoom Meeting Transcriber & Minute Generator...")
    logger.info(f"Diretório base detectado: {get_base_dir()}")

    # Pré-carrega variáveis de ambiente
    api_key = load_api_key_from_env()
    if api_key:
        logger.info("Chave GEMINI_API_KEY detectada no ambiente.")
    else:
        logger.warning("GEMINI_API_KEY não localizada. O modal de configuração será exibido na interface.")

    # Inicia a GUI
    run_gui()


if __name__ == "__main__":
    main()
