"""Serviço do Escutaí de integração com a API do Google Gemini (gemini-2.5-flash).

Gerencia fila assíncrona de blocos de áudio, upload via Files API,
transcrição incremental estéreo (Online L/R) ou mono (Presencial em sala),
revisão fono-semântica em segunda passada e síntese consolidada da Ata Executiva.
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
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from archive_service import MeetingArchiveService
from offline_asr_service import OfflineAsrService
from usage_tracker import UsageTracker
from voiceprint_service import VoiceprintService

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

logger = logging.getLogger("escutai.gemini_service")


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

    TEXT_FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash")

    TRANSCRIPTION_SYSTEM_PROMPT = """Você é o motor de transcrição inteligente e diarizador acústico de alta precisão do Escutaí.
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
   - REGRA CRÍTICA ANTI-LOOP: Nunca repita palavras, gaguejos, partículas ("que o, que o...") ou turnos idênticos em ciclo repetitivo; em hesitações, registre no máximo 1 vez (ou `[incompreensível]`) e avance no áudio. Mantenha estritamente o idioma português e o formato `[MM:SS] **Falante**: <fala>` do início ao fim (nunca mude para cabeçalhos `**MM:SS - MM:SS**` nem verbos em inglês como `speaks.`).
"""

    PRESENTIAL_TRANSCRIPTION_PROMPT = """Você é o motor de transcrição inteligente e diarizador acústico de alta precisão do Escutaí.
O áudio fornecido em WAV MONO (1 canal) foi captado por um único microfone de ambiente em uma **REUNIÃO PRESENCIAL** de sala única (com múltiplos participantes falando ao redor do mesmo microfone).

DIRETRIZES FUNDAMENTAIS DE DIARIZAÇÃO ACÚSTICA EM SALA ÚNICA:
1. IDENTIFICAÇÃO DE VOZES E NOMES:
   - Diferencie cuidadosamente os locutores pelo **timbre vocal, altura (pitch), cadência, gênero, distância acústica do microfone e contexto da conversa**.
   - Rotule como **Willian** sempre que o falante for identificado pelo contexto, autoapresentação, direcionamento direto de outro participante ("Willian, o que você acha?") ou continuidade vocal já estabelecida nos blocos anteriores.
   - Para os demais participantes da sala ainda sem nome citado, mantenha rotulagem numérica consistente entre turnos e blocos:
     [MM:SS] **Participante 1**: <texto falado>
     [MM:SS] **Participante 2**: <texto falado>
   - Extração Ativa de Nomes: assim que um nome for mencionado ("Aqui é o Marcos", "Concordo com a Ana"), associe permanentemente aquela voz ao nome real: `[MM:SS] **Marcos**: <texto>`.

2. TRATAMENTO CRÍTICO DE FALAS SOBREPOSTAS NA SALA (CROSS-TALK):
   - Em reuniões presenciais, duas ou mais pessoas frequentemente falam ao mesmo tempo, interrompem ou comentam em paralelo.
   - NUNCA funda vozes diferentes em um único parágrafo nem descarte a fala mais baixa ou distante.
   - Sempre que detectar sobreposição simultânea de vozes no canal mono, marque EXPLICITAMENTE com:
     [MM:SS] ⚡ [Falas Sobrepostas]:
       - **Willian**: <fala completa sem resumir>
       - **Participante 1**: <fala simultânea completa sem resumir>

3. FIDELIDADE LITERAL E FORMATAÇÃO:
   - Transcreva literalmente em português brasileiro com precisão máxima (>= 98%), mantendo siglas corporativas e termos técnicos em inglês intactos (ex.: Framework, Deploy, Pipeline, Query, API, BI).
   - Ignore ruídos de ambiente (ar-condicionado, cadeira, clique de mouse/teclado, papel).
   - Se os segundos iniciais (~2,5s) repetirem o final exato do contexto anterior por sobreposição de bloco, dê continuidade natural sem truncar palavras na fronteira.
   - Se o trecho inteiro não possuir fala humana perceptível, retorne exatamente: *[Silêncio / Sem fala identificada no período]*
   - Não resuma nem corrija livremente o estilo oral; transcreva todas as falas na íntegra.
   - REGRA CRÍTICA ANTI-LOOP: Nunca repita palavras, gaguejos, partículas ("que o, que o...") ou turnos idênticos em ciclo repetitivo; em hesitações, registre no máximo 1 vez (ou `[incompreensível]`) e avance no áudio. Mantenha estritamente o idioma português e o formato `[MM:SS] **Falante**: <fala>` do início ao fim (nunca mude para cabeçalhos `**MM:SS - MM:SS**` nem verbos em inglês como `speaks.`).
"""

    REFINEMENT_SYSTEM_PROMPT = """Você é um revisor especialista em transcrições corporativas e correção fono-semântica em português brasileiro e termos técnicos de TI/negócios.
Analise a transcrição bruta da reunião abaixo (composta por blocos sequenciais com ~2,5s de overlap de áudio entre blocos consecutivos) e devolva a transcrição revisada integral seguindo ESTRITAMENTE estas regras:

1. CORREÇÃO FONO-SEMÂNTICA:
   - Corrija erros fonéticos óbvios e palavras mal reconhecidas usando o contexto global da reunião e o vocabulário prioritário (ex.: termos técnicos, siglas, sistemas, nomes próprios mal grafados).
   - NÃO invente falas, NÃO parafraseie em excesso e NÃO altere o sentido original do que foi dito.

2. UNIFICAÇÃO RETROATIVA DE PARTICIPANTES:
   - Se o nome de algum participante foi descoberto apenas em blocos posteriores (ex.: `Participante 2` foi identificado como `Carlos` ou `Participante 1 (Voz masculina)` como `Lucas`), substitua o rótulo genérico pelo nome real em TODOS os blocos anteriores correspondentes da transcrição.
   - Preserve intactos os rótulos **Willian (Você)**, **Willian** e os marcadores `⚡ [Falas Sobrepostas...]:`.

3. REMOÇÃO DE DUPLICAÇÕES DE OVERLAP (2,5s):
   - Remova repetições exatas ou parciais de frases ocorridas na fronteira imediata entre o fim de um bloco (`### Bloco N`) e o início do bloco seguinte (`### Bloco N+1`) causadas pela janela de 2,5 segundos de sobreposição, mantendo a frase completa exatamente uma vez no ponto natural.

4. INTEGRIDADE ESTRUTURAL:
   - Preserve os cabeçalhos Markdown (`# Transcrição...`, `### Bloco ...`, `---`), timestamps `[MM:SS]` e TODOS os turnos de fala.
   - NUNCA resuma, omita ou condense o diálogo. Retorne o documento Markdown completo revisado.
{vocab_section}
--- TRANSCRIÇÃO BRUTA DA REUNIÃO: {title} ---
{raw_transcript}
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
        on_archive_generated: Optional[Callable[[Path, dict], None]] = None,
        defer_init: bool = False,
    ):
        self.api_key = api_key or load_api_key_from_env()
        self.on_log = on_log
        self.on_transcription_update = on_transcription_update
        self.on_minute_generated = on_minute_generated
        self.on_status_change = on_status_change
        self.on_archive_generated = on_archive_generated

        self._task_queue: queue.Queue[Optional[AudioBlockTask]] = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._is_running = False

        self._client = None
        if not defer_init:
            self._init_client()

        # Rastreador persistente, biometria vocal, motor offline (5.C) e pacote SQLite portátil (5.D)
        self.tracker = UsageTracker(get_base_dir() / "atas_reunioes")
        self.voiceprint_service = VoiceprintService()
        self.offline_asr_service = OfflineAsrService(voiceprint_service=self.voiceprint_service)
        self.archive_service = MeetingArchiveService()

        # Preferências de execução híbrida (online/offline) e empacotamento portátil
        self.engine_mode = "online"
        self.offline_whisper_variant = "turbo"
        self.offline_ollama_model = ""
        self.offline_isolate_voice = False
        self.offline_enhancement_mode = "clarify_room"
        self.mic_owner_filter_enabled = False
        self.mic_owner_profile_name = ""
        self.auto_export_sqlite = True
        self._offline_unknown_clusters = []

        # Metadados da reunião atual
        self.meeting_title = ""
        self.context_notes = ""
        self.meeting_start_time = datetime.now()
        self.transcription_file_path: Optional[Path] = None
        self.minute_file_path: Optional[Path] = None
        self.audio_file_path: Optional[Path] = None
        self.archive_file_path: Optional[Path] = None
        self.accumulated_transcript = ""
        self.last_minutes_text = ""
        self._max_block_end_seconds = 0.0

    def get_session_audio_path(self) -> Optional[Path]:
        """Retorna o caminho do arquivo WAV oficial da sessão atual."""
        return self.audio_file_path

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

    def start_session(
        self,
        title: str,
        context_notes: str = "",
        source_audio_path: Optional[Path] = None,
        engine_mode: Optional[str] = None,
    ) -> Tuple[Path, Path]:
        """Prepara os arquivos e pastas da sessão de reunião e inicia o worker."""
        if engine_mode in ("online", "offline"):
            self.engine_mode = engine_mode

        self.meeting_title = title.strip() or "Reuniao"
        self.context_notes = (context_notes or "").strip()
        self.meeting_start_time = datetime.now()
        self.accumulated_transcript = ""
        self.last_minutes_text = ""
        self._max_block_end_seconds = 0.0
        self._offline_unknown_clusters = []

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
        self.audio_file_path = (
            Path(source_audio_path).resolve()
            if source_audio_path is not None
            else (target_dir / f"audio_{timestamp_str}{suffix}.wav")
        )
        self.archive_file_path = target_dir / f"reuniao_{timestamp_str}{suffix}.escutai"

        # Inicializa o arquivo de transcrição com cabeçalho Markdown
        data_formatada = self.meeting_start_time.strftime("%d/%m/%Y às %H:%M")
        context_line = f"**Contexto / Glossário:** {self.context_notes}\n" if self.context_notes else ""
        audio_line = f"**Áudio Oficial:** `{self.audio_file_path.name}`\n" if self.audio_file_path else ""
        engine_desc = (
            f"Offline Local (Whisper {self.offline_whisper_variant} CT2/ONNX + Voiceprints)"
            if self.engine_mode == "offline"
            else "Online (Gemini 2.5 Flash + Voiceprints Locais)"
        )
        header_text = (
            f"# Transcrição da Reunião: {self.meeting_title}\n"
            f"**Data de Início:** {data_formatada}\n"
            f"**Arquivo de Registro:** `{self.transcription_file_path.name}`\n"
            f"{audio_line}"
            f"**Motor de Transcrição:** {engine_desc}\n"
            f"**Modo de Captura:** Detecção automática por bloco (Estéreo L/R Online ou Mono Presencial)\n"
            f"{context_line}\n"
            f"---\n\n"
        )
        with open(self.transcription_file_path, "w", encoding="utf-8") as f:
            f.write(header_text)

        self._log(f"[SESSÃO] Transcrição ({engine_desc}): {self.transcription_file_path.name}")

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
        self._max_block_end_seconds = max(self._max_block_end_seconds, float(end_seconds))
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

    def _detect_wav_channels(self, wav_path: Path) -> int:
        """Detecta automaticamente se o WAV possui 1 canal (Mono) ou 2 canais (Estéreo)."""
        try:
            with wave.open(str(wav_path), "rb") as wf:
                channels = int(wf.getnchannels())
                return 1 if channels == 1 else 2
        except Exception as e:
            logger.warning(f"Falha ao detectar canais de {wav_path}: {e}. Usando fallback Estéreo (2ch).")
            return 2

    def _select_transcription_prompt(self, num_channels: int) -> str:
        """Seleciona o prompt especializado conforme o layout de canais (1=Mono Presencial, 2=Estéreo Online)."""
        return self.PRESENTIAL_TRANSCRIPTION_PROMPT if num_channels == 1 else self.TRANSCRIPTION_SYSTEM_PROMPT

    _STANDARD_TURN_RE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s+\*\*(.+?)\*\*:\s*(.*)$")
    _INTRA_NGRAM_LOOP_RE = re.compile(
        r"(\b[\wÀ-ÿ]{1,24}(?:[,\s]+[\wÀ-ÿ]{1,24}){0,5}?\b)(?:[,\s]+\1){4,}",
        flags=re.IGNORECASE,
    )
    _BROKEN_RANGE_HEADER_RE = re.compile(r"^\s*\*\*(\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*\d{1,2}:\d{2}(?::\d{2})?\*\*\s*$")
    _ENGLISH_SPEAKS_LINE_RE = re.compile(r'^\s*[-*]\s+(.+?)\s+\bspeaks\.\s*(?:"(.*)"|(.*))$', flags=re.IGNORECASE)
    _ENGLISH_LEAK_RE = re.compile(r"\bthat'?s\s+that'?s\s+the\s+first\s+part\b[.!]*", flags=re.IGNORECASE)

    def _build_generation_config(
        self,
        temperature: float = 0.2,
        top_p: float = 0.95,
        max_output_tokens: Optional[int] = None,
    ):
        """Constrói GenerateContentConfig quando `google.genai.types` estiver carregado."""
        if types is None:
            _ensure_genai_loaded()
        if types is not None and hasattr(types, "GenerateContentConfig"):
            kwargs = {"temperature": temperature, "top_p": top_p}
            if max_output_tokens is not None and max_output_tokens > 0:
                kwargs["max_output_tokens"] = int(max_output_tokens)
            return types.GenerateContentConfig(**kwargs)
        return None

    @classmethod
    def _collapse_intraline_loops(cls, line: str) -> Tuple[str, int]:
        """Colapsa repetições consecutivas anômalas (5+x) de palavras/n-gramas dentro de uma linha."""
        removed_copies = 0

        def _repl(match: re.Match) -> str:
            nonlocal removed_copies
            unit = match.group(1).strip()
            total_span = len(match.group(0))
            unit_len = max(1, len(unit) + 1)
            est_copies = max(4, (total_span // unit_len) - 1)
            removed_copies += est_copies
            return unit

        cleaned = cls._INTRA_NGRAM_LOOP_RE.sub(_repl, line)
        if cls._ENGLISH_LEAK_RE.search(cleaned):
            cleaned = cls._ENGLISH_LEAK_RE.sub("", cleaned).strip()
            removed_copies += 4
        # Limpa sobras de pontuação após colapso ("..., , ..." ou vírgula solta no fim)
        cleaned = re.sub(r"(?:,\s*){2,}", ", ", cleaned)
        cleaned = re.sub(r",\s*\.", ".", cleaned)
        if removed_copies > 0:
            cleaned = re.sub(r",\s*$", "", cleaned)
        cleaned = cleaned.strip()
        return cleaned, removed_copies

    @classmethod
    def _normalize_broken_format(cls, text: str) -> Tuple[str, bool]:
        """Detecta e converte cabeçalhos `**MM:SS - MM:SS**` / `- Falante speaks.` para o padrão Escutaí."""
        out_lines: List[str] = []
        broken_detected = False
        pending_ts: Optional[str] = None

        for raw_line in text.splitlines():
            hdr_m = cls._BROKEN_RANGE_HEADER_RE.match(raw_line)
            if hdr_m:
                pending_ts = hdr_m.group(1)
                broken_detected = True
                continue

            spk_m = cls._ENGLISH_SPEAKS_LINE_RE.match(raw_line)
            if spk_m:
                broken_detected = True
                speaker = spk_m.group(1).strip().strip("*")
                spoken = (spk_m.group(2) if spk_m.group(2) is not None else spk_m.group(3) or "").strip()
                ts_prefix = f"[{pending_ts}] " if pending_ts else ""
                if speaker and spoken:
                    out_lines.append(f"{ts_prefix}**{speaker}**: {spoken}")
                continue

            if raw_line.strip():
                pending_ts = None
            out_lines.append(raw_line)

        return "\n".join(out_lines), broken_detected

    @classmethod
    def _turn_signature(cls, line: str) -> Optional[Tuple[str, str]]:
        """Extrai assinatura normalizada `(falante, fala)` ignorando timestamp `[MM:SS]`."""
        m = cls._STANDARD_TURN_RE.match(line.strip())
        if not m:
            return None
        speaker = re.sub(r"\s+", " ", m.group(2)).strip().lower()
        utterance = re.sub(r"[\s.,!?;:\"'`()\-]+", " ", m.group(3)).strip().lower()
        return speaker, utterance

    @classmethod
    def _deduplicate_turn_cycles(cls, text: str) -> Tuple[str, int]:
        """Remove loops consecutivos de turnos únicos ou alternados (ciclos de período 1 a 4)."""
        lines = text.splitlines()
        turns: List[Tuple[int, int, Tuple[str, str]]] = []
        run_id = 0
        for idx, raw_line in enumerate(lines):
            sig = cls._turn_signature(raw_line)
            if sig is not None:
                turns.append((idx, run_id, sig))
            elif raw_line.strip():
                # Cabeçalhos, notas ou linhas não-turno quebram contiguidade de ciclo
                run_id += 1

        if len(turns) < 3:
            return text, 0

        drop_indices: set[int] = set()
        insert_marker_after: set[int] = set()
        pruned_count = 0
        pos = 0
        n = len(turns)

        while pos < n:
            matched_cycle = False
            curr_run = turns[pos][1]
            for period in (1, 2, 3, 4):
                if pos + 3 * period > n:
                    continue
                if any(turns[pos + k][1] != curr_run for k in range(3 * period)):
                    continue
                base_sigs = [turns[pos + k][2] for k in range(period)]
                if period > 1 and not any(len(s[1]) >= 8 for s in base_sigs):
                    continue

                reps = 1
                while pos + (reps + 1) * period <= n:
                    if any(turns[pos + reps * period + k][1] != curr_run for k in range(period)):
                        break
                    cand_sigs = [turns[pos + reps * period + k][2] for k in range(period)]
                    if cand_sigs != base_sigs:
                        break
                    reps += 1

                min_reps = 3 if (period > 1 or len(base_sigs[0][1]) >= 10) else 4
                if reps >= min_reps:
                    last_kept_line_idx = turns[pos + period - 1][0]
                    for r in range(1, reps):
                        for k in range(period):
                            drop_indices.add(turns[pos + r * period + k][0])
                            pruned_count += 1
                    pos += reps * period
                    # Consome eventual prefixo incompleto pendente do próximo ciclo repetido (ex.: turno ímpar final)
                    tail_k = 0
                    while pos < n and tail_k < period and turns[pos][1] == curr_run and turns[pos][2] == base_sigs[tail_k]:
                        drop_indices.add(turns[pos][0])
                        pruned_count += 1
                        pos += 1
                        tail_k += 1
                    insert_marker_after.add(last_kept_line_idx)
                    matched_cycle = True
                    break
            if not matched_cycle:
                pos += 1

        if pruned_count == 0:
            return text, 0

        rebuilt: List[str] = []
        for idx, line in enumerate(lines):
            if idx in drop_indices:
                continue
            rebuilt.append(line)
            if idx in insert_marker_after:
                rebuilt.append("*[Trecho em repetição suprimido automaticamente]*")

        # Colapsa linhas em branco excessivas deixadas pela poda
        cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(rebuilt)).strip()
        return cleaned, pruned_count

    @classmethod
    def _detect_and_sanitize_repetition(cls, text: str) -> Tuple[str, bool, str]:
        """Detecta e saneia loops intra-linha, ciclos de turnos e desvios de formato do Gemini."""
        if not text or not text.strip():
            return text, False, ""

        normalized_text, broken_format = cls._normalize_broken_format(text)

        cleaned_lines: List[str] = []
        total_intraline_removed = 0
        for raw_line in normalized_text.splitlines():
            clean_line, removed = cls._collapse_intraline_loops(raw_line)
            total_intraline_removed += removed
            if clean_line.strip() or not raw_line.strip():
                cleaned_lines.append(clean_line)

        stage1 = "\n".join(cleaned_lines)
        stage2, pruned_turns = cls._deduplicate_turn_cycles(stage1)

        reasons: List[str] = []
        if total_intraline_removed >= 4:
            reasons.append(f"loop intra-linha ({total_intraline_removed}x)")
        if pruned_turns >= 2:
            reasons.append(f"ciclo de turnos ({pruned_turns} linhas)")
        if broken_format:
            reasons.append("desvio de formato/idioma")

        had_loop = bool(reasons)
        return stage2.strip() or text.strip(), had_loop, ", ".join(reasons)

    def _build_safe_recent_context(self, max_turns: int = 6) -> str:
        """Seleciona apenas turnos válidos e não-repetitivos recentes para guiar o próximo bloco."""
        if not self.accumulated_transcript.strip():
            return ""

        valid_turns: List[str] = []
        prev_sig: Optional[Tuple[str, str]] = None
        for raw_line in self.accumulated_transcript.splitlines():
            line = raw_line.strip()
            if not self._STANDARD_TURN_RE.match(line):
                continue
            sig = self._turn_signature(line)
            if sig == prev_sig:
                continue
            prev_sig = sig
            valid_turns.append(line)

        if not valid_turns:
            return ""

        last_turns = valid_turns[-max_turns:]
        return (
            "\n--- CONTEXTO DAS FALAS ANTERIORES (Use para manter nomes e diarização consistentes) ---\n"
            + "\n".join(last_turns)
            + "\n--------------------------------------------------------------------------------------\n\n"
        )

    def _build_priority_vocabulary_section(self) -> str:
        """Gera bloco de vocabulário prioritário a partir de `context_notes` e palavras-chave do `title`."""
        stopwords = {
            "de", "da", "do", "das", "dos", "e", "ou", "em", "para", "com", "sem",
            "por", "sobre", "entre", "reuniao", "reunião", "alinhamento", "call",
        }
        keywords: List[str] = []
        for token in re.findall(r"[A-Za-zÀ-ÿ0-9_\-]{3,}", self.meeting_title or ""):
            if token.lower() not in stopwords and token not in keywords:
                keywords.append(token)

        notes = (self.context_notes or "").strip()
        enrolled_names = [p["name"] for p in self.voiceprint_service.list_profiles()] if self.voiceprint_service else []
        if not keywords and not notes and not enrolled_names:
            return ""

        parts = ["\n--- VOCABULÁRIO PRIORITÁRIO E CONTEXTO DA REUNIÃO ---"]
        if self.meeting_title:
            parts.append(f"Título da Reunião: {self.meeting_title}")
        if enrolled_names:
            parts.append(f"Locutores com Voiceprint Cadastrado: {', '.join(enrolled_names)}")
        if keywords:
            parts.append(f"Palavras-chave do Título: {', '.join(keywords)}")
        if notes:
            parts.append(f"Glossário / Participantes / Notas de Contexto: {notes}")
        parts.append("Use estritamente estes termos, siglas e nomes próprios quando foneticamente compatíveis.")
        parts.append("-----------------------------------------------------\n")
        return "\n".join(parts) + "\n"

    @staticmethod
    def _extract_usage_tokens(response) -> Tuple[int, int]:
        """Extrai contagem segura de tokens de entrada e saída da resposta do Gemini."""
        in_tok = 0
        out_tok = 0
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            in_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
            out_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
        return int(in_tok), int(out_tok)

    def _generate_text_with_fallback(
        self,
        prompt: str,
        temperature: float = 0.2,
        top_p: float = 0.95,
        max_output_tokens: Optional[int] = None,
        is_new_meeting: bool = False,
    ) -> Tuple[Optional[str], str]:
        """Executa chamada leve de texto puro com fallback automático multi-modelo (429 / indisponibilidade)."""
        if self._client is None:
            if not self.api_key:
                self.api_key = load_api_key_from_env()
            if not self.api_key or not self._init_client():
                return None, ""

        gen_config = self._build_generation_config(
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_output_tokens,
        )

        for model_name in self.TEXT_FALLBACK_MODELS:
            for attempt in range(2):
                try:
                    kwargs = {
                        "model": model_name,
                        "contents": [prompt],
                    }
                    if gen_config is not None:
                        kwargs["config"] = gen_config
                    response = self._client.models.generate_content(**kwargs)
                    in_tok, out_tok = self._extract_usage_tokens(response)
                    self.tracker.register_request(
                        audio_seconds=0.0,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        is_new_meeting=is_new_meeting,
                    )
                    txt = response.text.strip() if getattr(response, "text", None) else ""
                    if txt:
                        return txt, model_name
                    break
                except Exception as exc:
                    err_low = str(exc).lower()
                    is_quota_or_missing = any(
                        tok in err_low
                        for tok in ("429", "resource_exhausted", "quota", "rate limit", "404", "not found")
                    )
                    logger.warning(f"Gemini texto ({model_name}, tentativa {attempt + 1}) falhou: {exc}")
                    if is_quota_or_missing:
                        self._log(f"[API] Modelo {model_name} indisponível/sem cota; tentando próximo modelo...")
                        break
                    if attempt == 0:
                        time.sleep(0.8)
                    else:
                        break
        return None, ""

    def _process_single_block_offline(self, task: AudioBlockTask, time_range: str, mode_label: str) -> None:
        """Transcreve um bloco WAV localmente usando Whisper ONNX + Voiceprints (sem API Gemini)."""
        try:
            ready, msg = self.offline_asr_service.is_ready(self.offline_whisper_variant)
            if not ready:
                self._set_status("Baixando modelo offline Whisper...")
                self._log(f"[OFFLINE] {msg} Iniciando download automático...")
                ok, dl_msg = self.offline_asr_service.ensure_models_ready(
                    variant=self.offline_whisper_variant,
                    progress_cb=lambda m, p: self._set_status(f"{m} ({int(p*100)}%)"),
                )
                if not ok:
                    err_md = f"### Bloco {task.block_index:02d} [{time_range}]\n\n*[Falha no modo offline: {dl_msg}. WAV preservado em: `{task.wav_path.name}`]*\n\n---\n\n"
                    self._append_to_transcription(task, err_md, success=False)
                    return

            self._set_status(f"Transcrevendo Bloco #{task.block_index} (Offline Whisper)...")
            enrolled_names = [p["name"] for p in self.voiceprint_service.list_profiles()] if self.voiceprint_service else []
            eff_mode = getattr(self, "offline_enhancement_mode", "clarify_room") or "clarify_room"
            raw_text = self.offline_asr_service.transcribe_wav_block(
                task.wav_path,
                block_offset_seconds=task.start_seconds,
                variant=self.offline_whisper_variant,
                session_unknown_clusters=self._offline_unknown_clusters,
                context_notes=f"{self.meeting_title} {self.context_notes}".strip(),
                enrolled_names=enrolled_names,
                isolate_voice=bool(self.offline_isolate_voice or eff_mode == "neural_voice_isolation"),
                clarify_audio=(eff_mode != "none"),
                enhancement_mode=eff_mode,
                mic_owner_filter_enabled=bool(self.mic_owner_filter_enabled),
                mic_owner_profile_name=str(self.mic_owner_profile_name or ""),
            )
            transcription_text, _, _ = self._detect_and_sanitize_repetition(raw_text)
            if not transcription_text:
                transcription_text = "*[Silêncio / Sem fala identificada no período]*"

            audio_dur = max(0.0, task.end_seconds - task.start_seconds)
            self.tracker.register_request(audio_seconds=audio_dur, input_tokens=0, output_tokens=0)

            block_markdown = (
                f"### Bloco {task.block_index:02d} [{time_range}]\n\n"
                f"{transcription_text}\n\n"
                f"---\n\n"
            )
            self._append_to_transcription(task, block_markdown, success=True)
            self._log(f"[OK] Bloco #{task.block_index} ({mode_label} • Offline Whisper) transcrito e salvo.")
        except Exception as e:
            self._log(f"[ERRO] Falha offline no Bloco #{task.block_index}: {e}")
            err_md = (
                f"### Bloco {task.block_index:02d} [{time_range}]\n\n"
                f"*[Falha offline: {e}. Áudio preservado em: `{task.wav_path.name}`]*\n\n---\n\n"
            )
            self._append_to_transcription(task, err_md, success=False)

    def _process_single_block(self, task: AudioBlockTask) -> None:
        """Transcreve um bloco de áudio (Online Gemini ou Offline Whisper), anexa no Markdown e limpa o WAV."""
        time_range = f"{format_seconds(task.start_seconds)} - {format_seconds(task.end_seconds)}"
        num_channels = self._detect_wav_channels(task.wav_path)
        mode_label = "Mono / Presencial" if num_channels == 1 else "Estéreo / Online"
        self._log(f"[IA] Processando Bloco #{task.block_index} ({time_range} • {self.engine_mode})...")
        self._log(f"[ÁUDIO] Bloco #{task.block_index} detectado: {num_channels} canal(is) ({mode_label}).")

        if self.engine_mode == "offline":
            self._process_single_block_offline(task, time_range, mode_label)
            return

        if not self._client:
            msg = f"\n### Bloco {task.block_index:02d} [{time_range}]\n*[Erro: Chave Gemini não configurada]*\n\n"
            self._append_to_transcription(task, msg, success=False)
            return

        system_prompt = self._select_transcription_prompt(num_channels)

        uploaded_remote_file = None
        filtered_wav_path: Optional[Path] = None
        upload_path = task.wav_path
        try:
            if num_channels >= 2 and self.mic_owner_filter_enabled:
                if self.voiceprint_service and self.voiceprint_service.is_ready_for_matching():
                    owner_name = self.voiceprint_service.resolve_owner_profile_name(self.mic_owner_profile_name)
                    if owner_name:
                        candidate_filtered = task.wav_path.with_name(f"{task.wav_path.stem}_micfiltered.wav")
                        try:
                            changed, mic_stats = self.voiceprint_service.filter_channel_to_speaker_wav(
                                task.wav_path,
                                owner_name=owner_name,
                                out_wav_path=candidate_filtered,
                                channel_index=1,
                            )
                            if changed and candidate_filtered.exists():
                                filtered_wav_path = candidate_filtered
                                upload_path = candidate_filtered
                                self._log(
                                    f"[BIOMETRIA MIC] Bloco #{task.block_index}: Canal R filtrado para '{owner_name}' "
                                    f"(mantidos {mic_stats['kept_s']:.1f}s, silenciados {mic_stats['muted_s']:.1f}s de terceiros)."
                                )
                        except Exception as mic_flt_err:
                            logger.warning(f"Falha no filtro biométrico do Canal R no bloco #{task.block_index}: {mic_flt_err}")
                    else:
                        self._log("[AVISO] Filtro biométrico do microfone ativo, mas nenhum perfil de usuário foi encontrado.")
                else:
                    self._log("[AVISO] Filtro biométrico do microfone ativo, mas os modelos/perfis de voiceprint não estão prontos.")

            # 1. Upload do WAV para a API Files do Gemini
            self._log(f"[UPLOAD] Enviando {upload_path.name} para a nuvem Gemini...")
            uploaded_remote_file = self._client.files.upload(file=str(upload_path))

            # 2. Chamada com temperature=0.2, limite de tokens proporcional e proteção anti-loop
            recent_context = self._build_safe_recent_context(max_turns=6)

            vocab_section = self._build_priority_vocabulary_section()
            biometric_hints = ""
            if self.voiceprint_service and self.voiceprint_service.is_ready_for_matching():
                try:
                    biometric_hints = self.voiceprint_service.analyze_wav_speakers(
                        upload_path,
                        block_offset_seconds=task.start_seconds,
                    )
                    if biometric_hints:
                        self._log(f"[BIOMETRIA] Bloco #{task.block_index}: pistas de voiceprint geradas localmente.")
                except Exception as bio_err:
                    logger.warning(f"Falha ao executar biometria vocal no bloco #{task.block_index}: {bio_err}")
                    biometric_hints = ""

            overlap_hint = (
                "Nota: Os ~2,5s iniciais deste bloco podem sobrepor o final do bloco anterior; mantenha a continuidade natural sem cortar palavras.\n"
                if task.block_index > 1
                else ""
            )
            prompt = (
                f"Transcreva este bloco de áudio da reunião ({mode_label}).\n"
                f"Trecho temporal: {time_range}.\n"
                f"{overlap_hint}"
                f"{vocab_section}"
                f"{biometric_hints}"
                f"{recent_context}"
                f"{system_prompt}"
            )

            audio_dur = max(0.0, task.end_seconds - task.start_seconds)
            block_max_tokens = min(8192, max(3072, int(max(60.0, audio_dur) * 35)))

            def _invoke_block(temp: float) -> Tuple[str, int, int]:
                cfg = self._build_generation_config(
                    temperature=temp,
                    top_p=0.95,
                    max_output_tokens=block_max_tokens,
                )
                req_kwargs = {
                    "model": "gemini-2.5-flash",
                    "contents": [uploaded_remote_file, prompt],
                }
                if cfg is not None:
                    req_kwargs["config"] = cfg
                resp = self._client.models.generate_content(**req_kwargs)
                req_in, req_out = self._extract_usage_tokens(resp)
                txt = resp.text.strip() if getattr(resp, "text", None) else ""
                return txt, req_in, req_out

            raw_text, in_tok, out_tok = _invoke_block(0.2)
            transcription_text, had_loop, loop_reason = self._detect_and_sanitize_repetition(raw_text)

            if had_loop and raw_text:
                self._log(
                    f"[AVISO] Bloco #{task.block_index}: {loop_reason} detectado; "
                    f"reexecutando 1x com temperature=0.45 para quebrar repetição..."
                )
                retry_raw, retry_in, retry_out = _invoke_block(0.45)
                in_tok += retry_in
                out_tok += retry_out
                if retry_raw:
                    retry_clean, retry_had_loop, _ = self._detect_and_sanitize_repetition(retry_raw)
                    # Prefere o retry se eliminou o loop ou preservou conteúdo sem quebra de formato
                    if not retry_had_loop or len(retry_clean) > len(transcription_text):
                        transcription_text = retry_clean

            if not transcription_text:
                transcription_text = "*[Áudio sem transcrição disponível]*"

            # Rastreamento de tokens e cota da API
            self.tracker.register_request(audio_seconds=audio_dur, input_tokens=in_tok, output_tokens=out_tok)

            # 3. Formatação no Markdown
            block_markdown = (
                f"### Bloco {task.block_index:02d} [{time_range}]\n\n"
                f"{transcription_text}\n\n"
                f"---\n\n"
            )

            # 4. Gravação segura no arquivo Markdown
            self._append_to_transcription(task, block_markdown, success=True)
            self._log(f"[OK] Bloco #{task.block_index} ({mode_label}) transcrito e salvo no Markdown.")

        except Exception as e:
            self._log(f"[ERRO] Falha ao transcrever Bloco #{task.block_index}: {e}")
            error_markdown = (
                f"### Bloco {task.block_index:02d} [{time_range}]\n\n"
                f"*[Falha na transcrição: {e}. Áudio original preservado em: `{task.wav_path.name}`]*\n\n"
                f"---\n\n"
            )
            self._append_to_transcription(task, error_markdown, success=False)

        finally:
            if filtered_wav_path is not None and filtered_wav_path.exists():
                try:
                    filtered_wav_path.unlink()
                except Exception:
                    pass
            # 5. Exclusão do arquivo remoto na nuvem
            if uploaded_remote_file is not None and self._client is not None:
                try:
                    self._client.files.delete(name=uploaded_remote_file.name)
                    self._log(f"[NUVEM] Arquivo remoto {uploaded_remote_file.name} deletado com sucesso.")
                except Exception as del_err:
                    logger.debug(f"Não foi possível remover arquivo remoto: {del_err}")

    def _refine_transcript_phonetics(self, raw_transcript: str) -> str:
        """Executa segunda passada fono-semântica (texto puro) para corrigir fonética, unificar nomes e deduplicar overlap."""
        if not raw_transcript or not raw_transcript.strip():
            return raw_transcript
        if not self._client and not self.api_key:
            return raw_transcript

        try:
            self._log("[REVISÃO] Executando segunda passada fono-semântica (correção, nomes e overlap)...")
            header_prefix = ""
            body_raw = raw_transcript
            if raw_transcript.lstrip().startswith("# Transcrição") and "\n---\n" in raw_transcript:
                split_idx = raw_transcript.find("\n---\n")
                header_end = split_idx + len("\n---\n")
                while header_end < len(raw_transcript) and raw_transcript[header_end] == "\n":
                    header_end += 1
                header_prefix = raw_transcript[:header_end]
                body_raw = raw_transcript[header_end:]

            if not body_raw.strip():
                return raw_transcript

            # Saneia eventuais loops antes da revisão para não distorcer a janela de contexto nem o limiar de tamanho
            body_clean, _, _ = self._detect_and_sanitize_repetition(body_raw)

            vocab_section = self._build_priority_vocabulary_section()
            prompt = self.REFINEMENT_SYSTEM_PROMPT.format(
                vocab_section=vocab_section,
                title=self.meeting_title or "Reunião",
                raw_transcript=body_clean,
            )

            refined_raw, used_model = self._generate_text_with_fallback(
                prompt, temperature=0.15, top_p=0.95
            )
            if not refined_raw:
                return header_prefix + body_clean + "\n" if body_clean != body_raw else raw_transcript

            refined_text, _, _ = self._detect_and_sanitize_repetition(refined_raw)
            # Proteção contra resposta vazia ou truncamento anômalo pela API (medido contra o corpo já sem loops)
            if not refined_text or len(refined_text) < int(0.35 * len(body_clean.strip())):
                logger.warning("Revisão fono-semântica retornou conteúdo curto/vazio; mantendo transcrição sanitizada.")
                return header_prefix + body_clean + "\n" if body_clean != body_raw else raw_transcript

            self._log(f"[REVISÃO] Passada fono-semântica concluída ({used_model}).")
            return header_prefix + refined_text + "\n"

        except Exception as e:
            logger.warning(f"Falha na revisão fono-semântica (usando transcrição bruta): {e}")
            self._log(f"[AVISO] Revisão fono-semântica falhou ({e}); prosseguindo com transcrição original.")
            return raw_transcript

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

    def _export_session_archive(self, transcript_md: str, minutes_md: str) -> None:
        """Exporta o pacote SQLite portátil `.escutai` contendo texto e áudio comprimido da reunião."""
        if not self.auto_export_sqlite or not self.archive_file_path:
            return
        try:
            self._set_status("Compactando pacote SQLite (.escutai)...")
            summary = self.archive_service.export_meeting_package(
                output_path=self.archive_file_path,
                title=self.meeting_title or "Reuniao",
                created_at=self.meeting_start_time,
                mode="Híbrido",
                engine_mode=self.engine_mode,
                context_notes=self.context_notes,
                transcript_md=transcript_md,
                minutes_md=minutes_md,
                wav_path=self.audio_file_path if (self.audio_file_path and self.audio_file_path.exists()) else None,
            )
            mb = summary["size_bytes"] / (1024 * 1024)
            ratio = summary["compression_ratio"]
            self._log(
                f"[SQLITE] Pacote portátil salvo: {self.archive_file_path.name} "
                f"({mb:.2f} MB • compressão {ratio}x • {summary['segment_count']} turnos)."
            )
            if self.on_archive_generated:
                self.on_archive_generated(self.archive_file_path, summary)
        except Exception as arch_err:
            logger.warning(f"Falha ao gerar pacote SQLite .escutai: {arch_err}")
            self._log(f"[AVISO] Não foi possível gerar pacote .escutai: {arch_err}")

    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        cleaned = (text or "").strip()
        cleaned = re.sub(r"^```(?:markdown|md)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned.strip()

    def _synthesize_minutes_hybrid_or_local(
        self,
        title: str,
        date_str: str,
        duration_str: str,
        transcript_text: str,
        allow_cloud_text: bool = True,
        register_new_meeting: bool = False,
    ) -> Tuple[str, str, bool]:
        """Tenta gerar Ata Executiva em 1 chamada leve de texto (multi-modelo); cai em <0.05s para síntese local."""
        prompt = self.MINUTES_SYSTEM_PROMPT.format(
            title=title or "Reunião",
            date_str=date_str,
            duration_str=duration_str,
            full_transcript=(transcript_text or "")[:65000],
        )

        if allow_cloud_text:
            ai_raw, used_model = self._generate_text_with_fallback(
                prompt,
                temperature=0.2,
                top_p=0.95,
                is_new_meeting=register_new_meeting,
            )
            if ai_raw:
                cleaned = self._strip_markdown_fences(ai_raw)
                if len(cleaned) > 150:
                    return cleaned + "\n", f"IA Nuvem ({used_model})", True

        self._set_status("Gerando Ata Executiva estruturada local...")
        local_text, local_backend = self.offline_asr_service.generate_offline_minutes(
            title=title or "Reunião",
            date_str=date_str,
            duration_str=duration_str,
            transcript_md=transcript_text,
            minutes_prompt_template=self.MINUTES_SYSTEM_PROMPT,
            preferred_ollama_model=self.offline_ollama_model,
        )
        if register_new_meeting:
            self.tracker.register_request(audio_seconds=0.0, input_tokens=0, output_tokens=0, is_new_meeting=True)
        return local_text, local_backend, False

    def regenerate_minutes_with_ai(
        self,
        transcript_text: str,
        title: Optional[str] = None,
        minute_file_path: Optional[Path] = None,
        transcript_file_path: Optional[Path] = None,
    ) -> Tuple[bool, str, str]:
        """Regenera a Ata Executiva sob demanda via 1 chamada leve de texto (com fallback multi-modelo/local)."""
        clean_src, _, _ = self._detect_and_sanitize_repetition(transcript_text or "")
        if not clean_src.strip():
            return False, "", "Transcrição vazia."

        eff_title = (title or "").strip()
        eff_date = ""
        eff_dur = ""
        m_title = re.search(r"^#\s*Transcri[çc][ãa]o da Reuni[ãa]o:\s*(.+)$", clean_src, flags=re.MULTILINE)
        if m_title and not eff_title:
            eff_title = m_title.group(1).strip()
        m_date = re.search(r"^\*\*Data de In[íi]cio:\*\*\s*(.+)$", clean_src, flags=re.MULTILINE)
        if m_date:
            eff_date = m_date.group(1).strip()
        timestamps = re.findall(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]", clean_src, flags=re.MULTILINE)
        if timestamps:
            parts = [int(p) for p in timestamps[-1].split(":")]
            last_secs = parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]
            eff_dur = format_seconds(last_secs)

        eff_title = eff_title or self.meeting_title or "Reunião"
        eff_date = eff_date or datetime.now().strftime("%d/%m/%Y às %H:%M")
        eff_dur = eff_dur or "Não informada"

        minute_text, backend_used, used_cloud = self._synthesize_minutes_hybrid_or_local(
            title=eff_title,
            date_str=eff_date,
            duration_str=eff_dur,
            transcript_text=clean_src,
            allow_cloud_text=True,
            register_new_meeting=False,
        )

        target_path = minute_file_path or self.minute_file_path
        if target_path is None and transcript_file_path is not None:
            t_path = Path(transcript_file_path)
            if t_path.name.startswith("transcricao_"):
                target_path = t_path.with_name(t_path.name.replace("transcricao_", "ata_executiva_", 1))

        if target_path is not None:
            target_path = Path(target_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(minute_text)
            self.minute_file_path = target_path

        self.last_minutes_text = minute_text
        self._log(f"[ATA SOB DEMANDA] Ata atualizada via {backend_used}.")
        return used_cloud, minute_text, backend_used

    def _generate_executive_minutes(self) -> None:
        """Lê a transcrição consolidada, gera a Ata Executiva (Híbrida/Online ou Offline) e salva o pacote `.escutai`."""
        if not self.transcription_file_path or not self.minute_file_path:
            self._log("[ERRO] Caminhos de arquivos de Ata não definidos.")
            return

        if self.engine_mode == "online" and not self._client and not self.api_key:
            self._log("[AVISO] Chave Gemini ausente; Ata Executiva não pôde ser gerada via IA.")
            fallback_text = (
                f"# Ata Executiva da Reunião: {self.meeting_title}\n"
                f"*(Não gerada automaticamente: configure sua GEMINI_API_KEY para gerar atas por IA)*\n\n"
                f"Consulte o arquivo de transcrição completa em: `{self.transcription_file_path.name}`\n"
            )
            with open(self.minute_file_path, "w", encoding="utf-8") as f:
                f.write(fallback_text)
            self.last_minutes_text = fallback_text
            if self.on_minute_generated:
                self.on_minute_generated(self.minute_file_path, fallback_text)
            return

        try:
            now = datetime.now()
            wall_secs = (now - self.meeting_start_time).total_seconds()
            duration_secs = max(wall_secs, self._max_block_end_seconds)
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
                self.last_minutes_text = notice_text
                if self.on_minute_generated:
                    self.on_minute_generated(self.minute_file_path, notice_text)
                return

            # Lê o conteúdo completo da transcrição gravada
            with open(self.transcription_file_path, "r", encoding="utf-8") as f:
                raw_transcript = f.read()

            if self.engine_mode == "offline":
                self.offline_asr_service.unload_asr_models()
                sanitized_raw, _, _ = self._detect_and_sanitize_repetition(raw_transcript)
                enrolled_names = (
                    [p["name"] for p in self.voiceprint_service.list_profiles()]
                    if self.voiceprint_service
                    else []
                )
                # Só executa se houver servidor Ollama externo rodando; caso contrário retorna em 0.0s
                refined_transcript = self.offline_asr_service.refine_offline_transcript(
                    sanitized_raw,
                    title=self.meeting_title,
                    context_notes=self.context_notes,
                    enrolled_names=enrolled_names,
                    preferred_ollama_model=self.offline_ollama_model,
                )
                if refined_transcript != raw_transcript and refined_transcript.strip():
                    with open(self.transcription_file_path, "w", encoding="utf-8") as f:
                        f.write(refined_transcript)
                    self.accumulated_transcript = refined_transcript
                    if self.on_transcription_update:
                        self.on_transcription_update(0, "", refined_transcript, True)

                self._set_status("Gerando Ata Executiva (Híbrido texto / Síntese local)...")
                minute_text, backend_used, used_cloud = self._synthesize_minutes_hybrid_or_local(
                    title=self.meeting_title,
                    date_str=date_str,
                    duration_str=duration_str,
                    transcript_text=refined_transcript,
                    allow_cloud_text=True,
                    register_new_meeting=True,
                )
                if used_cloud and "Modo Híbrido" not in minute_text:
                    minute_text += (
                        f"\n---\n*Ata sintetizada em modo Híbrido: Transcrição 100% Offline (Whisper Turbo CT2) "
                        f"+ Síntese Textual ({backend_used})*\n"
                    )

                with open(self.minute_file_path, "w", encoding="utf-8") as f:
                    f.write(minute_text)
                self.last_minutes_text = minute_text
                self._log(f"[SUCESSO] Ata Executiva salva ({backend_used}): {self.minute_file_path.name}")
                self._export_session_archive(refined_transcript, minute_text)
                if self.on_minute_generated:
                    self.on_minute_generated(self.minute_file_path, minute_text)
                return

            # Segunda passada fono-semântica antes da Ata Executiva (apenas texto)
            self._set_status("Refinando transcrição e nomes de participantes...")
            refined_transcript = self._refine_transcript_phonetics(raw_transcript)
            if refined_transcript != raw_transcript and refined_transcript.strip():
                with open(self.transcription_file_path, "w", encoding="utf-8") as f:
                    f.write(refined_transcript)
                self.accumulated_transcript = refined_transcript
                self._log(f"[REVISÃO] Transcrição revisada salva em: {self.transcription_file_path.name}")

            self._set_status("Gerando Ata Executiva consolidada...")
            self._log("[IA] Enviando transcrição revisada para síntese da Ata Executiva...")
            minute_text, backend_used, _used_cloud = self._synthesize_minutes_hybrid_or_local(
                title=self.meeting_title,
                date_str=date_str,
                duration_str=duration_str,
                transcript_text=refined_transcript,
                allow_cloud_text=True,
                register_new_meeting=True,
            )

            # Salva no arquivo de Ata Executiva
            with open(self.minute_file_path, "w", encoding="utf-8") as f:
                f.write(minute_text)
            self.last_minutes_text = minute_text

            self._log(f"[SUCESSO] Ata Executiva salva ({backend_used}) em: {self.minute_file_path.name}")
            self._export_session_archive(refined_transcript, minute_text)

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
