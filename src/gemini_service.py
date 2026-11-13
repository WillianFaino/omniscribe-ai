"""Serviço OmniScribe AI de integração com a API do Google Gemini (gemini-2.5-flash).

Gerencia fila assíncrona de blocos de áudio, upload via Files API,
transcrição incremental estéreo (Participantes vs Willian), higienização de arquivos locais/remotos
e síntese consolidada da Ata Executiva ao final da reunião.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from usage_tracker import UsageTracker

# Importação defensiva do python-dotenv com parser nativo de fallback
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(dotenv_path=None, override=True):
        if dotenv_path and Path(dotenv_path).exists():
            try:
                with open(dotenv_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            key = k.strip()
                            val = v.strip().strip("'\"")
                            if override or key not in os.environ:
                                os.environ[key] = val
            except Exception:
                pass

# Importação sob demanda (Lazy Loading) do SDK oficial google-genai para inicialização instantânea
genai = None
types = None
HAS_GOOGLE_GENAI: Optional[bool] = None


def _ensure_genai_loaded() -> bool:
    """Carrega o SDK google-genai apenas quando necessário, evitando bloqueio no boot da GUI."""
    global genai, types, HAS_GOOGLE_GENAI
    if HAS_GOOGLE_GENAI is not None:
        return HAS_GOOGLE_GENAI
    try:
        from google import genai as _genai
        from google.genai import types as _types
        genai = _genai
        types = _types
        HAS_GOOGLE_GENAI = True
    except ImportError:
        genai = None
        types = None
        HAS_GOOGLE_GENAI = False
    return HAS_GOOGLE_GENAI

logger = logging.getLogger("omniscribe.gemini_service")


def get_base_dir() -> Path:
    """Retorna o diretório base da aplicação (compatível com script e executável PyInstaller)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def sanitize_title(title: str) -> str:
    """Sanitiza o título da reunião para composição segura de nomes de arquivo (sem acentos e caracteres inválidos)."""
    normalized = unicodedata.normalize("NFKD", title.strip()).encode("ASCII", "ignore").decode("utf-8")
    clean = re.sub(r'[\\/*?:"<>|]', "", normalized)
    clean = re.sub(r"\s+", "_", clean)
    clean = re.sub(r"[^a-zA-Z0-9_\-]", "", clean)
    return clean[:45]


def load_api_key_from_env() -> str:
    """Carrega a chave da API Gemini a partir do .env no diretório base ou do ambiente."""
    env_path = get_base_dir() / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)
    return os.getenv("GEMINI_API_KEY", "").strip()


def save_api_key_to_env(api_key: str) -> None:
    """Salva a chave GEMINI_API_KEY no arquivo .env local no diretório base."""
    api_key_clean = api_key.strip()
    env_path = get_base_dir() / ".env"
    lines = []
    found = False

    if env_path.exists():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_lines = []
            for line in lines:
                if line.strip().startswith("GEMINI_API_KEY="):
                    new_lines.append(f"GEMINI_API_KEY={api_key_clean}\n")
                    found = True
                else:
                    new_lines.append(line)
            lines = new_lines
        except Exception as e:
            logger.warning(f"Erro ao ler .env existente: {e}")

    if not found:
        lines.append(f"GEMINI_API_KEY={api_key_clean}\n")

    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.environ["GEMINI_API_KEY"] = api_key_clean
        logger.info(f"Chave da API Gemini salva com sucesso em: {env_path}")
    except Exception as e:
        logger.exception(f"Erro ao salvar .env: {e}")
        raise


def format_seconds(seconds: float) -> str:
    """Converte segundos para formato MM:SS ou HH:MM:SS."""
    secs = int(max(0, seconds))
    hours = secs // 3600
    minutes = (secs % 3600) // 60
    rem_secs = secs % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{rem_secs:02d}"
    return f"{minutes:02d}:{rem_secs:02d}"


@dataclass
class AudioBlockTask:
    wav_path: Path
    block_index: int
    start_seconds: float
    end_seconds: float
    is_final: bool


class GeminiService:
    """Gerenciador do pipeline de transcrição incremental e consolidação da Ata."""

    TRANSCRIPTION_SYSTEM_PROMPT = """Você é o motor de transcrição inteligente e diarizador acústico de alta precisão do OmniScribe AI.
O áudio fornecido em WAV estéreo possui exatamente 2 canais sincronizados:
- CANAL DIREITO (Right / R): Microfone local do usuário -> SEMPRE identificado como **Willian (Você)**.
- CANAL ESQUERDO (Left / L): Áudio do sistema (Reuniões Google Meet/Teams/Zoom/Discord, vídeos do YouTube, podcasts ou palestras).

DIRETRIZES FUNDAMENTAIS DE DIARIZAÇÃO E IDENTIFICAÇÃO DE VOZES:
1. CANAL DIREITO (R) - Willian (Você):
   - Toda fala originada neste canal DEVE ser rotulada como:
     [MM:SS] **Willian (Você)**: <texto falado>

2. CANAL ESQUERDO (L) - IDENTIFICAÇÃO INTELIGENTE DE INTERLOCUTORES:
   - Extração Ativa de Nomes: Fique atento a apresentações pessoais ("Fala pessoal, aqui é o Lucas", "Bem-vindo ao canal do TechCode, sou o Marcos") e menções diretas de terceiros ("O que você acha, Juliana?", "Valeu, Roberto"). Assim que um nome for identificado, associe essa voz a esse NOME REAL permanentemente (ex.: [MM:SS] **Lucas**: <texto>, [MM:SS] **Juliana**: <texto>).
   - Vídeos do YouTube, Podcasts e Palestras: Se o áudio do sistema for uma transmissão, aula ou vídeo com um anfitrião/palestrante e o nome ainda não tiver sido dito, rotule como **Apresentador / Host** ou **Palestrante** em vez do genérico 'Participante A'.
   - Múltiplos Participantes Remotos (Diarização Acústica): Diferencie as vozes no Canal L pelo timbre, tom e gênero. Mantenha numeração consistente ao longo dos blocos:
     [MM:SS] **Participante 1 (Voz masculina)**: <texto>
     [MM:SS] **Participante 2 (Voz feminina)**: <texto>
     (Mantenha a coerência com as falas anteriores informadas no bloco de contexto).

3. TRATAMENTO DE FALAS SOBREPOSTAS E INTERRUPÇÕES (CROSS-TALK):
   - Em reuniões, debates e reacts de vídeos, é comum pessoas falarem ao mesmo tempo ou interromperem umas às outras.
   - Como os canais L e R são fisicamente isolados em estéreo, você é capaz de ouvir AMBAS as falas mesmo quando acontecem no exato mesmo segundo.
   - Quando Willian falar por cima do participante remoto (ou vice-versa), NUNCA descarte ou omita nenhuma das falas. Formate como falas simultâneas:
     [MM:SS] ⚡ [Falas Sobrepostas / Interrupção]:
       - **Willian (Você)**: <fala no microfone>
       - **<Nome/Apresentador>**: <fala no sistema ao mesmo tempo>
   - Se duas pessoas no Canal Esquerdo (sistema) falarem ao mesmo tempo, transcreva ambas claramente:
     [MM:SS] ⚡ [Falas Sobrepostas no Sistema]:
       - **Participante 1**: <texto>
       - **Participante 2**: <texto>

4. FIDELIDADE E FORMATAÇÃO:
   - Transcreva com fidelidade em português brasileiro, mantendo termos técnicos em inglês (ex.: Framework, Deploy, Pipeline, Bug, Query, API).
   - Ignore chiados estáticos, ruído elétrico ou respiração.
   - Se o trecho inteiro não possuir fala humana perceptível, retorne exatamente: *[Silêncio / Sem fala identificada no período]*
   - Não resuma nem omita palavras nesta etapa; forneça o diálogo completo.
"""

    MINUTES_SYSTEM_PROMPT = """Você é um secretário executivo sênior e analista corporativo de alto nível, especialista na elaboração de Atas Executivas para diretoria, reuniões técnicas e sessões de alinhamento.
Abaixo está a transcrição integral e sequencial da reunião (ou vídeo/podcast analisado).

Sua missão é produzir uma ATA EXECUTIVA completa, profissional e altamente acionável em Markdown, estruturada estritamente com as seguintes seções obrigatórias:

# Ata Executiva da Reunião: {title}
**Data:** {date_str}
**Duração Total:** {duration_str}
**Participantes Identificados:** Willian (Você) e demais participantes identificados pelo nome ou papel (ex.: Apresentador, Host, etc.)

---

## 1. Resumo Executivo
(Síntese concisa de 2 a 3 parágrafos destacando o objetivo central da reunião, os principais alinhamentos e os resultados obtidos)

## 2. Principais Tópicos Discutidos
(Detalhamento cronológico ou temático dos tópicos debatidos, contextualizando discussões, argumentos e conclusões de cada assunto com menção de horários/blocos)

## 3. Decisões Tomadas
(Lista com marcadores de todas as decisões formais e consensos atingidos pelos participantes durante a conversa)

## 4. Plano de Ação e Próximos Passos
(Tabela Markdown obrigatória e completa contendo as tarefas atribuídas:
| Tarefa / Ação | Responsável | Prazo | Prioridade |
| :--- | :--- | :--- | :--- |
| ... | ... | ... | ... |)

## 5. Pendências, Pontos de Atenção e Debates
(Riscos, bloqueios técnicos, dúvidas não resolvidas e pontos de desacordo ou debate durante falas sobrepostas. Destaque COM ÊNFASE em negrito quaisquer menções diretas a responsabilidades de Willian)

---
Transcrição completa da reunião:
{full_transcript}
"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_transcription_update: Optional[Callable[[int, str, str, bool], None]] = None,
        on_minute_generated: Optional[Callable[[Path, str], None]] = None,
        on_status_change: Optional[Callable[[str], None]] = None,
        defer_init: bool = False,
    ):
        self.api_key = api_key or load_api_key_from_env()
        self.on_log = on_log
        self.on_transcription_update = on_transcription_update
        self.on_minute_generated = on_minute_generated
        self.on_status_change = on_status_change

        self._task_queue: queue.Queue[Optional[AudioBlockTask]] = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._is_running = False

        self._client = None
        if not defer_init:
            self._init_client()

        # Rastreador persistente de cotas e custos
        self.tracker = UsageTracker(get_base_dir() / "atas_reunioes")

        # Metadados da reunião atual
        self.meeting_title = ""
        self.meeting_start_time = datetime.now()
        self.transcription_file_path: Optional[Path] = None
        self.minute_file_path: Optional[Path] = None
        self.accumulated_transcript = ""

    def get_usage_summary(self) -> dict:
        """Retorna resumo estatístico de consumo e custos."""
        return self.tracker.get_summary()

    def _log(self, message: str) -> None:
        logger.info(message)
        if self.on_log:
            try:
                self.on_log(message)
            except Exception:
                pass

    def _set_status(self, status: str) -> None:
        if self.on_status_change:
            try:
                self.on_status_change(status)
            except Exception:
                pass

    def _init_client(self) -> bool:
        if not _ensure_genai_loaded():
            self._log("[AVISO] SDK 'google-genai' não está instalado no ambiente.")
            return False

        if not self.api_key:
            self._log("[AVISO] GEMINI_API_KEY não configurada.")
            self._client = None
            return False

        try:
            self._client = genai.Client(api_key=self.api_key)
            self._log("[OK] Cliente Google Gemini (gemini-2.5-flash) inicializado.")
            return True
        except Exception as e:
            self._log(f"[ERRO] Falha ao inicializar cliente Gemini: {e}")
            self._client = None
            return False

    def update_api_key(self, new_key: str) -> bool:
        """Atualiza a chave da API em runtime e salva no .env."""
        save_api_key_to_env(new_key)
        self.api_key = new_key.strip()
        return self._init_client()

    def start_session(self, title: str) -> Tuple[Path, Path]:
        """Prepara os arquivos e pastas da sessão de reunião e inicia o worker."""
        self.meeting_title = title.strip() or "Reuniao"
        self.meeting_start_time = datetime.now()
        self.accumulated_transcript = ""

        # Diretório atas_reunioes/AAAA/MM/
        base_dir = get_base_dir()
        year_str = self.meeting_start_time.strftime("%Y")
        month_str = self.meeting_start_time.strftime("%m")
        target_dir = base_dir / "atas_reunioes" / year_str / month_str
        target_dir.mkdir(parents=True, exist_ok=True)

        clean_title = sanitize_title(self.meeting_title)
        timestamp_str = self.meeting_start_time.strftime("%Y%m%d_%H%M")
        suffix = f"_{clean_title}" if clean_title else ""

        self.transcription_file_path = target_dir / f"transcricao_{timestamp_str}{suffix}.md"
        self.minute_file_path = target_dir / f"ata_executiva_{timestamp_str}{suffix}.md"

        # Inicializa o arquivo de transcrição com cabeçalho Markdown
        data_formatada = self.meeting_start_time.strftime("%d/%m/%Y às %H:%M")
        header_text = (
            f"# Transcrição da Reunião: {self.meeting_title}\n"
            f"**Data de Início:** {data_formatada}\n"
            f"**Arquivo de Registro:** `{self.transcription_file_path.name}`\n"
            f"**Canais de Áudio:** Esquerdo (L) = Participantes / Zoom | Direito (R) = Willian (Você)\n\n"
            f"---\n\n"
        )
        with open(self.transcription_file_path, "w", encoding="utf-8") as f:
            f.write(header_text)

        self._log(f"[SESSÃO] Arquivo de transcrição criado: {self.transcription_file_path.name}")

        # Inicia thread de processamento da fila
        self._is_running = True
        self._worker_thread = threading.Thread(
            target=self._process_queue_loop, daemon=True, name="GeminiWorker"
        )
        self._worker_thread.start()

        return self.transcription_file_path, self.minute_file_path

    def enqueue_block(
        self,
        wav_path: Path,
        block_index: int,
        start_seconds: float,
        end_seconds: float,
        is_final: bool = False,
    ) -> None:
        """Coloca um bloco de áudio gerado pelo motor na fila de processamento."""
        task = AudioBlockTask(
            wav_path=wav_path,
            block_index=block_index,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            is_final=is_final,
        )
        self._task_queue.put(task)
        self._log(f"[FILA] Bloco #{block_index} enfileirado para transcrição.")

    def finish_session(self) -> None:
        """Sinaliza o término da reunião e aguarda a fila ser processada."""
        self._log("[SESSÃO] Aguardando conclusão da fila e geração da Ata Executiva...")
        self._task_queue.put(None)  # Sentinela de término

    def _process_queue_loop(self) -> None:
        """Loop contínuo de processamento da fila de blocos de áudio."""
        logger.debug("Iniciando loop do Gemini Worker.")

        has_final_block = False

        while self._is_running:
            try:
                task = self._task_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if task is None:
                # Sinal de término recebido
                self._log("[FILA] Todos os blocos enfileirados foram consumidos.")
                break

            self._set_status(f"Transcrevendo Bloco #{task.block_index}...")
            self._process_single_block(task)
            self._task_queue.task_done()

            if task.is_final:
                has_final_block = True

        # Ao esvaziar a fila, gera a Ata Executiva consolidada
        self._set_status("Gerando Ata Executiva consolidada...")
        self._generate_executive_minutes()
        self._set_status("Concluído")
        self._is_running = False
        logger.debug("Gemini Worker encerrado.")

    def _process_single_block(self, task: AudioBlockTask) -> None:
        """Transcreve um bloco de áudio via Gemini 2.5 Flash, anexa no Markdown e limpa o WAV."""
        time_range = f"{format_seconds(task.start_seconds)} - {format_seconds(task.end_seconds)}"
        self._log(f"[IA] Processando Bloco #{task.block_index} ({time_range})...")

        if not self._client:
            msg = f"\n### Bloco {task.block_index:02d} [{time_range}]\n*[Erro: Chave Gemini não configurada]*\n\n"
            self._append_to_transcription(task, msg, success=False)
            return

        uploaded_remote_file = None
        try:
            # 1. Upload do WAV para a API Files do Gemini
            self._log(f"[UPLOAD] Enviando {task.wav_path.name} para a nuvem Gemini...")
            uploaded_remote_file = self._client.files.upload(file=str(task.wav_path))

            # 2. Chamada de geração com gemini-2.5-flash
            recent_context = ""
            if self.accumulated_transcript.strip():
                # Coleta os últimos turnos de fala significativos para manter continuidade de vozes e nomes
                lines = [
                    l.strip()
                    for l in self.accumulated_transcript.strip().split("\n")
                    if l.strip() and not l.startswith("###") and not l.startswith("---") and not l.startswith("*[")
                ]
                last_turns = lines[-8:] if len(lines) > 8 else lines
                if last_turns:
                    recent_context = (
                        "\n--- CONTEXTO DAS FALAS ANTERIORES (Use para manter nomes e diarização consistentes) ---\n"
                        + "\n".join(last_turns)
                        + "\n--------------------------------------------------------------------------------------\n\n"
                    )

            prompt = (
                f"Transcreva este bloco de áudio da reunião.\n"
                f"Trecho temporal: {time_range}.\n"
                f"{recent_context}"
                f"{self.TRANSCRIPTION_SYSTEM_PROMPT}"
            )

            response = self._client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[uploaded_remote_file, prompt],
            )

            transcription_text = response.text.strip() if response.text else "*[Áudio sem transcrição disponível]*"

            # Rastreamento de tokens e cota da API
            in_tok = 0
            out_tok = 0
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                in_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                out_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
            audio_dur = max(0.0, task.end_seconds - task.start_seconds)
            self.tracker.register_request(audio_seconds=audio_dur, input_tokens=in_tok, output_tokens=out_tok)

            # 3. Formatação no Markdown
            block_markdown = (
                f"### Bloco {task.block_index:02d} [{time_range}]\n\n"
                f"{transcription_text}\n\n"
                f"---\n\n"
            )

            # 4. Gravação segura no arquivo Markdown
            self._append_to_transcription(task, block_markdown, success=True)
            self._log(f"[OK] Bloco #{task.block_index} transcrito e salvo no Markdown.")

        except Exception as e:
            self._log(f"[ERRO] Falha ao transcrever Bloco #{task.block_index}: {e}")
            error_markdown = (
                f"### Bloco {task.block_index:02d} [{time_range}]\n\n"
                f"*[Falha na transcrição: {e}. Áudio original preservado em: `{task.wav_path.name}`]*\n\n"
                f"---\n\n"
            )
            self._append_to_transcription(task, error_markdown, success=False)

        finally:
            # 5. Exclusão do arquivo remoto na nuvem
            if uploaded_remote_file is not None and self._client is not None:
                try:
                    self._client.files.delete(name=uploaded_remote_file.name)
                    self._log(f"[NUVEM] Arquivo remoto {uploaded_remote_file.name} deletado com sucesso.")
                except Exception as del_err:
                    logger.debug(f"Não foi possível remover arquivo remoto: {del_err}")

    def _append_to_transcription(self, task: AudioBlockTask, content: str, success: bool) -> None:
        """Anexa o texto ao arquivo Markdown e apaga o WAV local se a escrita foi bem-sucedida."""
        if not self.transcription_file_path:
            return

        try:
            with open(self.transcription_file_path, "a", encoding="utf-8") as f:
                f.write(content)

            self.accumulated_transcript += content

            # Dispara atualização para a GUI
            if self.on_transcription_update:
                self.on_transcription_update(
                    task.block_index,
                    content,
                    self.accumulated_transcript,
                    task.is_final,
                )

            # Regra crítica de negócio: apagar WAV local APENAS se a escrita foi confirmada
            if success:
                try:
                    task.wav_path.unlink(missing_ok=True)
                    self._log(f"[LIMPEZA] Arquivo temporário {task.wav_path.name} removido do disco.")
                except Exception as del_err:
                    logger.warning(f"Erro ao remover WAV local {task.wav_path.name}: {del_err}")
            else:
                self._log(f"[BACKUP] Arquivo {task.wav_path.name} mantido em disco para segurança.")

        except Exception as e:
            logger.exception(f"Erro ao escrever no arquivo de transcrição: {e}")

    def _generate_executive_minutes(self) -> None:
        """Lê a transcrição consolidada e gera a Ata Executiva estruturada via Gemini 2.5 Flash."""
        if not self.transcription_file_path or not self.minute_file_path:
            self._log("[ERRO] Caminhos de arquivos de Ata não definidos.")
            return

        if not self._client:
            self._log("[AVISO] Chave Gemini ausente; Ata Executiva não pôde ser gerada via IA.")
            fallback_text = (
                f"# Ata Executiva da Reunião: {self.meeting_title}\n"
                f"*(Não gerada automaticamente: configure sua GEMINI_API_KEY para gerar atas por IA)*\n\n"
                f"Consulte o arquivo de transcrição completa em: `{self.transcription_file_path.name}`\n"
            )
            with open(self.minute_file_path, "w", encoding="utf-8") as f:
                f.write(fallback_text)
            if self.on_minute_generated:
                self.on_minute_generated(self.minute_file_path, fallback_text)
            return

        try:
            now = datetime.now()
            duration_secs = (now - self.meeting_start_time).total_seconds()
            duration_str = format_seconds(duration_secs)
            date_str = self.meeting_start_time.strftime("%d/%m/%Y às %H:%M")

            # Verifica se houve alguma transcrição real acumulada
            if not self.accumulated_transcript.strip():
                self._log("[AVISO] Nenhum áudio/bloco transcrito registrado durante a sessão.")
                notice_text = (
                    f"# Ata Executiva da Reunião: {self.meeting_title}\n\n"
                    f"**Data:** {date_str}\n"
                    f"**Duração:** {duration_str}\n\n"
                    f"*[Nenhum bloco de áudio com fala foi detectado durante o período gravado.]*\n\n"
                    f"---\n\n"
                    f"### Observação do Sistema\n"
                    f"A gravação foi iniciada e encerrada sem dados de áudio acumulados no período.\n"
                    f"Certifique-se de reproduzir áudio (Zoom/YouTube/Teams) na saída selecionada ou falar ao microfone.\n"
                )
                with open(self.minute_file_path, "w", encoding="utf-8") as f:
                    f.write(notice_text)
                if self.on_minute_generated:
                    self.on_minute_generated(self.minute_file_path, notice_text)
                return

            # Lê o conteúdo completo da transcrição gravada
            with open(self.transcription_file_path, "r", encoding="utf-8") as f:
                full_transcript = f.read()

            prompt = self.MINUTES_SYSTEM_PROMPT.format(
                title=self.meeting_title,
                date_str=date_str,
                duration_str=duration_str,
                full_transcript=full_transcript,
            )

            self._log("[IA] Enviando transcrição integral para síntese da Ata Executiva...")
            response = self._client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[prompt],
            )

            minute_text = response.text.strip() if response.text else "# Ata Executiva\n*(Falha ao gerar texto)*"

            # Rastreamento de tokens da Ata Executiva
            in_tok = 0
            out_tok = 0
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                in_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                out_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
            self.tracker.register_request(audio_seconds=0.0, input_tokens=in_tok, output_tokens=out_tok, is_new_meeting=True)

            # Salva no arquivo de Ata Executiva
            with open(self.minute_file_path, "w", encoding="utf-8") as f:
                f.write(minute_text)

            self._log(f"[SUCESSO] Ata Executiva salva com sucesso em: {self.minute_file_path.name}")

            if self.on_minute_generated:
                self.on_minute_generated(self.minute_file_path, minute_text)

        except Exception as e:
            self._log(f"[ERRO] Falha ao sintetizar Ata Executiva: {e}")
            error_text = (
                f"# Ata Executiva da Reunião: {self.meeting_title}\n\n"
                f"**Ocorreu um erro ao gerar a ata executiva via IA:** `{e}`\n\n"
                f"A transcrição original está salva e preservada em: `{self.transcription_file_path.name}`.\n"
            )
            try:
                with open(self.minute_file_path, "w", encoding="utf-8") as f:
                    f.write(error_text)
                if self.on_minute_generated:
                    self.on_minute_generated(self.minute_file_path, error_text)
            except Exception:
                pass
