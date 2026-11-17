"""Motor de Transcrição Offline de Alta Precisão e Síntese do Escutaí.

Pipeline local sem dependência obrigatória de internet ou LLMs locais pesados:
1. Estágio 1 (Durante a gravação / blocos):
   - Ramo A (timbre natural 16 kHz): Silero VAD + biometria VoiceprintService
     (com inércia em segmentos curtos e suavização de 2 passadas);
   - Ramo B (ASR restaurado): isolamento neural GTCRN (`gtcrn_simple.onnx`) + EQ vocal
     multiestágio com colarinho de borda (+240 ms) para não cortar sílabas finais;
   - Decodificação local via `faster-whisper` (CTranslate2 INT8 com `AVX2`/`AVX-VNNI`,
     speech-packing <= 28s, `beam_size=2`, `word_timestamps=True`, `initial_prompt` neutro)
     com fallback automático para `sherpa-onnx` Whisper ONNX.
2. Estágio 2 (Ao finalizar a sessão):
   - Descarrega o modelo Whisper + GTCRN da memória RAM (`unload_asr_models`);
   - Se houver servidor Ollama externo rodando (`127.0.0.1:11434`), utiliza-o opcionalmente;
   - Caso contrário (100% offline sem LLM local pesado), gera instantaneamente (`< 0.05s`)
     uma Ata Executiva Estruturada em 5 seções via síntese determinística ou, no modo
     híbrido do `GeminiService`, envia apenas o texto em 1 requisição leve (~4s).
"""

from __future__ import annotations

import gc
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import threading
import time
import types
import unicodedata
import urllib.error
import urllib.request
import wave
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from audio_engine import TARGET_SAMPLE_RATE, enhance_speech_audio, resample_to_16k
from voiceprint_service import (
    DEFAULT_MATCH_THRESHOLD,
    DEFAULT_OVERLAP_MARGIN,
    VoiceprintService,
    _to_float32_mono,
)

logger = logging.getLogger("escutai.offline_asr")

DENOISER_MODEL_NAME = "gtcrn_simple.onnx"
DENOISER_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speech-enhancement-models/gtcrn_simple.onnx"
)

# Modelos CTranslate2 (faster-whisper) com suporte a beam_size=5, initial_prompt e hotwords
CT2_WHISPER_MODELS: Dict[str, Dict[str, Any]] = {
    "turbo": {
        "label": "Whisper Large-v3 Turbo CT2 INT8 (~780 MB • Máxima Precisão PT-BR)",
        "dir_name": "faster-whisper-large-v3-turbo-int8",
        "repo_url": "https://huggingface.co/Zoont/faster-whisper-large-v3-turbo-int8-ct2/resolve/main",
        "files": ["config.json", "preprocessor_config.json", "tokenizer.json", "vocabulary.json", "model.bin"],
    },
    "small": {
        "label": "Whisper Small CT2 (~465 MB • Equilibrado PT-BR)",
        "dir_name": "faster-whisper-small",
        "repo_url": "https://huggingface.co/Systran/faster-whisper-small/resolve/main",
        "files": ["config.json", "tokenizer.json", "vocabulary.txt", "model.bin"],
    },
    "base": {
        "label": "Whisper Base CT2 (~145 MB • Mais leve/rápido)",
        "dir_name": "faster-whisper-base",
        "repo_url": "https://huggingface.co/Systran/faster-whisper-base/resolve/main",
        "files": ["config.json", "tokenizer.json", "vocabulary.txt", "model.bin"],
    },
}

# Catálogo legado sherpa-onnx mantido como fallback automático caso CTranslate2 não esteja disponível
WHISPER_MODELS: Dict[str, Dict[str, str]] = {
    "turbo": {
        "label": "Whisper Large-v3 Turbo INT8 (~538 MB • Máxima Precisão PT-BR)",
        "dir_name": "sherpa-onnx-whisper-turbo",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-turbo.tar.bz2",
        "prefix": "turbo",
    },
    "small": {
        "label": "Whisper Small INT8 (~250 MB • Equilibrado PT-BR)",
        "dir_name": "sherpa-onnx-whisper-small",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-small.tar.bz2",
        "prefix": "small",
    },
    "base": {
        "label": "Whisper Base INT8 (~110 MB • Mais leve/rápido)",
        "dir_name": "sherpa-onnx-whisper-base",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-base.tar.bz2",
        "prefix": "base",
    },
}

_WHISPER_NOISE_RE = re.compile(
    r"^(?:\[.*\]|\(.*\)|legendas?\b.*|obrigad[oa] por assistir[!.]*|\.{2,}|\s*|"
    r"([a-zA-ZÀ-ÿ])\1{4,}[.!?]*)$",
    flags=re.IGNORECASE,
)
_TURN_RE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s+\*\*(.+?)\*\*:\s*(.*)$")
_OVERLAP_TURN_RE = re.compile(r"^\s*-\s+\*\*(.+?)\*\*:\s*(.*)$")
_DECISION_CUES = (
    "decid", "definid", "combinad", "fechad", "aprovad", "acordad", "vamos ",
    "ficou ", "precisa", "prazo", "entregar", "amanhã", "segunda", "terça",
    "quarta", "quinta", "sexta", "deploy", "corrigir", "criar", "subir",
    "olhar", "revisar", "documentar", "ajustar", "mapeamento", "cronograma",
    "organizar", "puxar", "validar", "levantar", "começar",
)
_PAIN_CUES = (
    "problema", "gargalo", "difícil", "dificuldade", "manual", "demora",
    "erro", "falha", "inconsist", "atras", "complex", "complicad",
    "trabalhos", "dor", "não está", "não tem", "falta ", "bagunç",
)
_BACKCHANNEL_SET = {
    "sim", "não", "nao", "tá", "ta", "ok", "certo", "exato", "exatamente",
    "uhum", "aham", "pois é", "verdade", "beleza", "show", "perfeito",
    "entendi", "aha", "claro", "legal", "faz sentido", "bom", "isso",
    "obrigado", "obrigada", "valeu", "tudo bem", "oi", "olá",
}
_BUILTIN_VOCABULARY = (
    "Escutaí",
    "OmniScribe",
    "Vozito",
    "ClickUp",
    "Bizagi",
    "BPMN",
    "AS-IS",
    "TO-BE",
    "WASAPI",
    "Voiceprint",
    "Whisper",
    "Gemini",
    "Ollama",
    "Power BI",
    "Controlsul",
    "inadimplência",
    "Excel",
    "Google Drive",
    "mapeamento",
    "cronograma",
    "fluxograma",
)


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _format_mmss(seconds: float) -> str:
    secs = int(max(0, round(seconds)))
    hours = secs // 3600
    mins = (secs % 3600) // 60
    rem = secs % 60
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{rem:02d}"
    return f"{mins:02d}:{rem:02d}"


def _collapse_phonetic_stutters(text: str) -> Tuple[str, bool]:
    """Colapsa gagueiras/loops de decodificação onde 4+ palavras seguidas partilham o mesmo prefixo."""
    tokens = text.split()
    if len(tokens) < 4:
        return text, False
    stopwords = {"de", "da", "do", "das", "dos", "e", "o", "a", "os", "as", "em", "no", "na", "que", "se", "um", "uma", "é"}
    stems: List[str] = []
    for tok in tokens:
        core = re.sub(r"[^\wÀ-ÿ]", "", tok).lower()
        if core in stopwords or len(core) < 3:
            stems.append("")
        else:
            ph = _phonetic_norm(core)
            stems.append(ph[:3] if len(ph) >= 3 else core[:3])

    out_tokens: List[str] = []
    did_stutter = False
    i = 0
    while i < len(tokens):
        st = stems[i]
        if st:
            # Conta ocorrências do mesmo radical em uma janela contígua de até 14 tokens
            j = i + 1
            matches = [i]
            non_empty_since_last = 0
            while j < len(tokens) and non_empty_since_last <= 3:
                if stems[j] == st:
                    matches.append(j)
                    non_empty_since_last = 0
                elif stems[j]:
                    non_empty_since_last += 1
                j += 1
            if len(matches) >= 4:
                did_stutter = True
                # Mantém apenas até a primeira frase fechada (ou os primeiros 2 tokens do padrão)
                keep_end = matches[0] + 1
                while keep_end < matches[1] and not tokens[keep_end - 1].endswith((".", "?", "!")):
                    keep_end += 1
                out_tokens.extend(tokens[i:keep_end])
                i = matches[-1] + 1
                continue
        out_tokens.append(tokens[i])
        i += 1
    return " ".join(out_tokens), did_stutter


def _clean_whisper_text(raw_text: str) -> str:
    text = re.sub(r"\s+", " ", (raw_text or "")).strip()
    if not text or _WHISPER_NOISE_RE.match(text):
        return ""
    # Remove marcadores fechados ou truncados (ex.: "[MÚSICA", "[Som de", "(risos)", "[aplausos]")
    text = re.sub(
        r"\s*[\(\[](?:risos?|m[úu]sica|aplausos?|sil[êe]ncio|som|barulho|ru[íi]do|music|laughter|applause|noise|sound)(?:\s+[^\]\)]{1,30})?(?:[\)\]]|$)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    # Colapsa alongamentos absurdos (ex.: "Eeeeeeeeeeeeeeeee" -> vazio se isolado)
    text = re.sub(r"\b([a-zA-ZÀ-ÿ])\1{4,}[.!?,;]*\b", "", text, flags=re.IGNORECASE)
    # Colapsa alucinações curtas repetidas do Whisper (ex.: "Obrigado. Obrigado. Obrigado.")
    text = re.sub(r"(\b[\wÀ-ÿ]{2,20}[.!?]?)(?:\s+\1){3,}", r"\1", text, flags=re.IGNORECASE)
    # Remove cadeias alucinadas de valores monetários (vazamento de decoder sem âncora)
    text = re.sub(r"(?:\bR\$\s*[\d.,]+\s*[,;.]?\s*){2,}", " ", text, flags=re.IGNORECASE)
    # Remove ecos em lista de nomes de participantes/glossário separados por vírgula
    text = re.sub(
        r"(?:Dr\.\s+|L[óo]grafo,?\s*|Fiqueir[ao],?\s*)?(?:(?:Jonathan Angeli|Raiana Deparis|Willian Faino|Irio Moesch|Giorgio Reuni[ãa]o|Raiano Departamento)\s*[,;.]\s*){2,}.*$",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text, stuttered = _collapse_phonetic_stutters(text)
    if stuttered or len(text) <= 28:
        text = re.sub(r"(?:^|\s+)(?:\bObrigado[.!?]?\s*)+$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ,;-")
    if not text or (len(text) <= 2 and not text.isalnum()) or _WHISPER_NOISE_RE.match(text):
        return ""
    return text


def _phonetic_norm(value: str) -> str:
    """Normaliza uma palavra/expressão para esqueleto fonético PT-BR."""
    s = unicodedata.normalize("NFKD", (value or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]", "", s)
    s = s.replace("y", "i").replace("w", "u")
    s = re.sub(r"^h+", "", s)
    s = s.replace("ph", "f").replace("ch", "x").replace("sh", "x")
    s = re.sub(r"qu([ei])", r"k\1", s)
    s = re.sub(r"c([ei])", r"s\1", s)
    s = s.replace("c", "k").replace("ç", "s").replace("z", "s")
    s = re.sub(r"(.)\1+", r"\1", s)
    return s


def _extract_glossary_terms(context_notes: str = "", enrolled_names: Optional[List[str]] = None) -> List[str]:
    """Constrói lista ordenada de termos prioritários para ancoragem ASR e pós-ASR."""
    ignored = {"participantes", "participante", "termos", "termo", "glossario", "glossário", "reuniao", "reunião"}
    terms: List[str] = []
    seen_low = set()

    def _add(item: str) -> None:
        cleaned = re.sub(r"\s+", " ", (item or "")).strip(" .,:;-•")
        if len(cleaned) < 4 or cleaned.lower() in ignored or cleaned.lower() in seen_low:
            return
        seen_low.add(cleaned.lower())
        terms.append(cleaned)

    for nm in (enrolled_names or []):
        _add(nm)
        for part in nm.split():
            if len(part) >= 4:
                _add(part)

    if context_notes:
        for seg in re.split(r"[,;\n/|]+", context_notes):
            clean_seg = re.sub(r"^(?:participantes?|termos?|gloss[áa]rio|nomes?)\s*:\s*", "", seg.strip(), flags=re.IGNORECASE)
            _add(clean_seg)
            for tok in re.findall(r"[A-Za-zÀ-ÿ0-9_\-]{4,}", clean_seg):
                _add(tok)

    for builtin in _BUILTIN_VOCABULARY:
        _add(builtin)

    terms.sort(key=lambda t: (-len(t.split()), -len(t)))
    return terms


def _apply_glossary_and_phonetic_fixes(
    text: str,
    context_notes: str = "",
    enrolled_names: Optional[List[str]] = None,
) -> str:
    """Corrige termos foneticamente próximos ao glossário do usuário e nomes cadastrados."""
    if not text:
        return text

    terms = _extract_glossary_terms(context_notes, enrolled_names)
    ctx_all_low = (f"{context_notes or ''} {' '.join(enrolled_names or [])}").lower()

    # Correções diretas comuns em falas PT-BR corporativas e desvios fonéticos recorrentes
    out = re.sub(r"\bHomens\s+Square\b", "OmniScribe", text, flags=re.IGNORECASE)
    out = re.sub(r"\bOmnis?\s+Square\b", "OmniScribe", out, flags=re.IGNORECASE)
    out = re.sub(r"\b(?:o\s+)?tio\s+bia\b", "To-Be", out, flags=re.IGNORECASE)
    out = re.sub(r"\b2\s*B\b", "To-Be", out, flags=re.IGNORECASE)
    out = re.sub(r"\b(?:rede\s+de\s+apl[êe]ncia|rede\s+de\s+adimpl[êe]ncia)\b", "inadimplência", out, flags=re.IGNORECASE)
    out = re.sub(r"\bpapai\s+de\s+frota\b", "painel de frota", out, flags=re.IGNORECASE)
    out = re.sub(r"\bparte\s+frota\b", "parte de painel de frota", out, flags=re.IGNORECASE)
    out = re.sub(r"\bpessoal\s+inferno\b", "pessoal interno", out, flags=re.IGNORECASE)
    out = re.sub(r"\bexcel\s+playlist\b", "planilha de Excel", out, flags=re.IGNORECASE)
    out = re.sub(r"\bintranetico\b", "sistema interno", out, flags=re.IGNORECASE)
    out = re.sub(r"\bdesse\s+processos\b", "desse processo", out, flags=re.IGNORECASE)
    out = re.sub(r"\bUnión\b", "União", out)

    if "raiana" in ctx_all_low:
        out = re.sub(r"\b(?:Ryan|Nyan|Raian|Raiane)\b", "Raiana", out, flags=re.IGNORECASE)
    if "clickup" in ctx_all_low or "click up" in ctx_all_low:
        out = re.sub(r"\b(?:pick-up|pickup|clique\s+up|click\s+app)\b", "ClickUp", out, flags=re.IGNORECASE)
    if "bizagi" in ctx_all_low:
        out = re.sub(r"\b(?:bisagi|biza\s*gi|biza)\b", "Bizagi", out, flags=re.IGNORECASE)
    if "bpmn" in ctx_all_low:
        out = re.sub(r"\b(?:bpm\s+n|bpn)\b", "BPMN", out, flags=re.IGNORECASE)

    # Matching fonético geral em janelas de 1 a 2 palavras contra os termos cadastrados
    term_entries = [(t, _phonetic_norm(t), len(t.split())) for t in terms]
    term_entries = [(t, ph, wc) for t, ph, wc in term_entries if len(ph) >= 4]
    if not term_entries:
        return out

    words = out.split()
    if not words:
        return out

    result: List[str] = []
    i = 0
    while i < len(words):
        replaced = False
        for span_len in (2, 1):
            if i + span_len > len(words):
                continue
            raw_span = " ".join(words[i : i + span_len])
            lead_match = re.match(r"^([^\wÀ-ÿ]*)(.*?)([^\wÀ-ÿ]*)$", raw_span)
            if not lead_match:
                continue
            pre_p, core, post_p = lead_match.group(1), lead_match.group(2), lead_match.group(3)
            if len(core) < 4:
                continue
            ph_core = _phonetic_norm(core)
            if len(ph_core) < 4:
                continue

            best_term = None
            best_ratio = 0.0
            for term, ph_term, wc in term_entries:
                if wc != span_len:
                    continue
                if core.lower() == term.lower():
                    best_term = None
                    break
                thresh = 0.80 if len(ph_term) >= 7 else 0.88
                r = SequenceMatcher(None, ph_core, ph_term).ratio()
                if r >= thresh and r > best_ratio:
                    best_ratio = r
                    best_term = term
            if best_term:
                result.append(f"{pre_p}{best_term}{post_p}")
                i += span_len
                replaced = True
                break
        if not replaced:
            result.append(words[i])
            i += 1
    return " ".join(result)


def _safe_extract_tar_bz2(archive_path: Path, dest_root: Path) -> None:
    dest_resolved = dest_root.resolve()
    with tarfile.open(str(archive_path), "r:bz2") as tf:
        for member in tf.getmembers():
            target = (dest_resolved / member.name).resolve()
            if not target.is_relative_to(dest_resolved):
                raise RuntimeError(f"Entrada inválida no arquivo tar: {member.name}")
        tf.extractall(path=str(dest_resolved))


def _import_faster_whisper():
    """Importa faster_whisper sem exigir PyAV/onnxruntime/huggingface_hub instalados."""
    if "av" not in sys.modules:
        try:
            import av  # noqa: F401
        except ImportError:
            sys.modules["av"] = types.ModuleType("av")
    if "huggingface_hub" not in sys.modules:
        try:
            import huggingface_hub  # noqa: F401
        except ImportError:
            sys.modules["huggingface_hub"] = types.ModuleType("huggingface_hub")
    import faster_whisper  # type: ignore

    return faster_whisper


def _download_file_parallel(
    url: str,
    dest_path: Path,
    progress_cb: Optional[Callable[[str, float], None]] = None,
    label: str = "",
    base_pct: float = 0.0,
    span_pct: float = 1.0,
    num_workers: int = 8,
) -> None:
    """Baixa arquivo via HTTP Range paralelo quando suportado (fallback automático para stream único)."""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_name(dest_path.name + ".part.tmp")
    headers = {"User-Agent": "Escutai/2.0"}
    lbl = label or dest_path.name

    supports_range = False
    total_bytes = 0
    try:
        probe_req = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-0"})
        with urllib.request.urlopen(probe_req, timeout=20) as resp:
            status = getattr(resp, "status", 200)
            c_range = resp.headers.get("Content-Range") or ""
            if status == 206 and "/" in c_range:
                tot_str = c_range.rsplit("/", 1)[-1].strip()
                if tot_str.isdigit():
                    total_bytes = int(tot_str)
                    supports_range = True
            elif resp.headers.get("Content-Length"):
                cl = resp.headers.get("Content-Length", "0").strip()
                if cl.isdigit():
                    total_bytes = int(cl)
    except Exception:
        supports_range = False
        total_bytes = 0

    last_report = [0.0]
    done_lock = threading.Lock()
    downloaded_bytes = [0]

    def _emit_progress(done: int, force: bool = False) -> None:
        if not progress_cb:
            return
        now = time.monotonic()
        if not force and (now - last_report[0]) < 0.25:
            return
        last_report[0] = now
        if total_bytes > 0:
            frac = min(1.0, done / float(total_bytes))
            mb_done = done / (1024 * 1024)
            mb_tot = total_bytes / (1024 * 1024)
            progress_cb(
                f"{lbl} ({mb_done:.1f}/{mb_tot:.1f} MB)",
                base_pct + span_pct * frac,
            )
        else:
            mb_done = done / (1024 * 1024)
            progress_cb(f"{lbl} ({mb_done:.1f} MB)", base_pct + span_pct * 0.5)

    try:
        if supports_range and total_bytes >= 8 * 1024 * 1024 and num_workers > 1:
            with open(tmp_path, "wb") as f:
                f.truncate(total_bytes)

            chunk_size = max(4 * 1024 * 1024, (total_bytes + num_workers * 4 - 1) // (num_workers * 4))
            ranges: List[Tuple[int, int]] = []
            pos = 0
            while pos < total_bytes:
                end = min(total_bytes - 1, pos + chunk_size - 1)
                ranges.append((pos, end))
                pos = end + 1

            write_lock = threading.Lock()

            def _fetch_part(rng: Tuple[int, int]) -> None:
                start_b, end_b = rng
                expected = end_b - start_b + 1
                last_err: Optional[Exception] = None
                for _attempt in range(3):
                    try:
                        req = urllib.request.Request(
                            url,
                            headers={**headers, "Range": f"bytes={start_b}-{end_b}"},
                        )
                        with urllib.request.urlopen(req, timeout=90) as resp:
                            if getattr(resp, "status", 200) != 206:
                                raise RuntimeError(f"Servidor não retornou 206 para Range {start_b}-{end_b}")
                            buf = resp.read()
                        if len(buf) != expected:
                            raise RuntimeError(f"Tamanho divergente ({len(buf)} != {expected})")
                        with write_lock:
                            with open(tmp_path, "r+b") as wf:
                                wf.seek(start_b)
                                wf.write(buf)
                        with done_lock:
                            downloaded_bytes[0] += expected
                            _emit_progress(downloaded_bytes[0])
                        return
                    except Exception as exc:
                        last_err = exc
                        time.sleep(0.6)
                raise RuntimeError(f"Falha ao baixar bloco {start_b}-{end_b}: {last_err}")

            try:
                with ThreadPoolExecutor(max_workers=num_workers) as pool:
                    list(pool.map(_fetch_part, ranges))
                if tmp_path.stat().st_size != total_bytes:
                    raise RuntimeError("Arquivo incompleto após download paralelo.")
                _emit_progress(total_bytes, force=True)
                tmp_path.replace(dest_path)
                return
            except Exception as par_err:
                logger.warning(f"Download paralelo falhou ({par_err}); tentando stream único...")
                downloaded_bytes[0] = 0

        # Fallback: stream HTTP único
        req_single = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req_single, timeout=300) as resp:
            cl = resp.headers.get("Content-Length") or "0"
            if total_bytes <= 0 and cl.isdigit():
                total_bytes = int(cl)
            with open(tmp_path, "wb") as f:
                while True:
                    block = resp.read(512 * 1024)
                    if not block:
                        break
                    f.write(block)
                    downloaded_bytes[0] += len(block)
                    _emit_progress(downloaded_bytes[0])
        if total_bytes > 0 and tmp_path.stat().st_size != total_bytes:
            raise RuntimeError(f"Download incompleto ({tmp_path.stat().st_size}/{total_bytes} bytes)")
        _emit_progress(downloaded_bytes[0], force=True)
        tmp_path.replace(dest_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def list_ollama_models(base_url: str = "http://127.0.0.1:11434", timeout: float = 1.2) -> Tuple[bool, List[str]]:
    """Verifica rapidamente se o servidor Ollama local está ativo e lista modelos de chat disponíveis."""
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        models = []
        for entry in data.get("models", []):
            name = str(entry.get("name", "")).strip()
            low = name.lower()
            if name and not any(tok in low for tok in ("embed", "nomic", "mxbai", "bge-")):
                models.append(name)
        return True, models
    except Exception:
        return False, []


class OfflineAsrService:
    """Serviço local de transcrição (Whisper CT2 + GTCRN), diarização biométrica e síntese."""

    def __init__(
        self,
        models_dir: Optional[Path] = None,
        voiceprint_service: Optional[VoiceprintService] = None,
        default_variant: str = "turbo",
    ):
        base_dir = _get_base_dir()
        meipass = getattr(sys, "_MEIPASS", None)

        if models_dir:
            p_models = Path(models_dir)
            self._custom_models_dir = True
            if p_models.name.lower() == "models" or (p_models / "asr").exists():
                models_root = p_models
                self.models_dir = models_root / "asr"
            else:
                models_root = p_models.parent
                self.models_dir = p_models
        else:
            self._custom_models_dir = False
            models_root = base_dir / "models"
            if not models_root.exists() and meipass and (Path(meipass) / "models").exists():
                models_root = Path(meipass) / "models"
            self.models_dir = models_root / "asr"

        self.models_root = models_root
        self.denoiser_dir = models_root / "denoiser"
        self.denoiser_model_path = self.denoiser_dir / DENOISER_MODEL_NAME

        self.voiceprint_service = voiceprint_service or VoiceprintService()
        self.default_variant = default_variant if default_variant in CT2_WHISPER_MODELS else "turbo"

        self._lock = threading.Lock()
        self._denoiser_lock = threading.Lock()
        self._recognizer = None
        self._ct2_model = None
        self._denoiser = None
        self._loaded_variant: Optional[str] = None
        self._loaded_ct2_variant: Optional[str] = None
        self._sherpa_onnx = None
        self._rolling_prompt_tail: str = ""

    def _candidate_models_roots(self) -> List[Path]:
        roots: List[Path] = [self.models_root]
        if not self._custom_models_dir:
            base_m = _get_base_dir() / "models"
            if base_m not in roots:
                roots.append(base_m)
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                mp_m = Path(meipass) / "models"
                if mp_m not in roots:
                    roots.append(mp_m)
            local_app = os.environ.get("LOCALAPPDATA", "")
            if local_app:
                inst_m = Path(local_app) / "Programs" / "Escutai" / "models"
                if inst_m not in roots:
                    roots.append(inst_m)
        return roots

    def _ensure_sherpa(self):
        if self._sherpa_onnx is None:
            import sherpa_onnx as _so
            self._sherpa_onnx = _so
        return self._sherpa_onnx

    @staticmethod
    def _can_import_faster_whisper() -> bool:
        try:
            _import_faster_whisper()
            return True
        except Exception:
            return False

    def _resolve_ct2_model_dir(self, variant: str) -> Optional[Path]:
        spec = CT2_WHISPER_MODELS.get(variant, CT2_WHISPER_MODELS["turbo"])
        cands = [self.models_dir / spec["dir_name"]]
        for root in self._candidate_models_roots():
            p = root / "asr" / spec["dir_name"]
            if p not in cands:
                cands.append(p)
        for model_dir in cands:
            model_bin = model_dir / "model.bin"
            cfg_json = model_dir / "config.json"
            if model_bin.is_file() and model_bin.stat().st_size > 50_000_000 and cfg_json.is_file():
                return model_dir
        return None

    def _resolve_model_files(self, variant: str) -> Optional[Tuple[Path, Path, Path]]:
        spec = WHISPER_MODELS.get(variant, WHISPER_MODELS["small"])
        cands = [self.models_dir / spec["dir_name"]]
        for root in self._candidate_models_roots():
            p = root / "asr" / spec["dir_name"]
            if p not in cands:
                cands.append(p)

        for model_dir in cands:
            if not model_dir.exists():
                continue
            prefix = spec["prefix"]
            enc_cands = [
                model_dir / f"{prefix}-encoder.int8.onnx",
                model_dir / f"{prefix}-encoder.onnx",
            ] + sorted(model_dir.rglob("*encoder*.onnx"))
            dec_cands = [
                model_dir / f"{prefix}-decoder.int8.onnx",
                model_dir / f"{prefix}-decoder.onnx",
            ] + sorted(model_dir.rglob("*decoder*.onnx"))
            tok_cands = [
                model_dir / f"{prefix}-tokens.txt",
            ] + sorted(model_dir.rglob("*tokens*.txt"))

            enc = next((p for p in enc_cands if p.is_file() and p.stat().st_size > 10000), None)
            dec = next((p for p in dec_cands if p.is_file() and p.stat().st_size > 10000), None)
            tok = next((p for p in tok_cands if p.is_file() and p.stat().st_size > 100), None)
            if enc and dec and tok:
                return enc, dec, tok
        return None

    def is_ready(self, variant: Optional[str] = None) -> Tuple[bool, str]:
        """Verifica se o motor CT2 (faster-whisper) ou fallback ONNX está pronto offline."""
        var = variant if variant in CT2_WHISPER_MODELS else self.default_variant
        if self._resolve_ct2_model_dir(var) is not None and self._can_import_faster_whisper():
            return True, f"Whisper {var} (CT2 INT8) pronto para uso 100% offline."

        if self._resolve_model_files(var) is not None:
            try:
                self._ensure_sherpa()
                return True, f"Whisper {var} (ONNX fallback) pronto para uso offline."
            except Exception as e:
                return False, f"Dependência sherpa-onnx indisponível: {e}"

        return False, f"Modelo offline Whisper ({var}) ainda não baixado em {self.models_dir}."

    def _ensure_ct2_models_ready(
        self,
        var: str,
        progress_cb: Optional[Callable[[str, float], None]] = None,
        base_pct: float = 0.08,
        span_pct: float = 0.37,
    ) -> Tuple[bool, str]:
        if self._resolve_ct2_model_dir(var) is not None and self._can_import_faster_whisper():
            return True, f"Modelo CT2 Whisper ({var}) já está pronto."
        if not self._can_import_faster_whisper():
            return False, "faster_whisper indisponível para CT2."

        spec = CT2_WHISPER_MODELS[var]
        target_dir = self.models_dir / spec["dir_name"]
        target_dir.mkdir(parents=True, exist_ok=True)
        repo_url = spec["repo_url"].rstrip("/")
        files: List[str] = list(spec["files"])
        small_files = [f for f in files if f != "model.bin"]

        try:
            for idx, fname in enumerate(small_files):
                fpath = target_dir / fname
                if fpath.is_file() and fpath.stat().st_size > 10:
                    continue
                _download_file_parallel(
                    f"{repo_url}/{fname}",
                    fpath,
                    progress_cb=progress_cb,
                    label=f"Baixando metadados Whisper {var} ({fname})",
                    base_pct=base_pct + (idx / max(1, len(small_files))) * (span_pct * 0.08),
                    span_pct=span_pct * 0.02,
                    num_workers=1,
                )

            bin_path = target_dir / "model.bin"
            if not (bin_path.is_file() and bin_path.stat().st_size > 50_000_000):
                _download_file_parallel(
                    f"{repo_url}/model.bin",
                    bin_path,
                    progress_cb=progress_cb,
                    label=f"Baixando Whisper {var} CT2 INT8",
                    base_pct=base_pct + span_pct * 0.08,
                    span_pct=span_pct * 0.92,
                    num_workers=8,
                )

            if self._resolve_ct2_model_dir(var) is None:
                return False, f"Arquivos CT2 Whisper inválidos em {target_dir}."
            return True, f"Whisper {var} (CT2 INT8) baixado com sucesso."
        except Exception as e:
            logger.warning(f"Falha no download CT2 ({var}): {e}")
            return False, f"Falha ao baixar Whisper {var} CT2: {e}"

    def ensure_models_ready(
        self,
        variant: Optional[str] = None,
        progress_cb: Optional[Callable[[str, float], None]] = None,
    ) -> Tuple[bool, str]:
        """Garante download de Voiceprints, Whisper (CT2 ou ONNX) e GTCRN Denoiser (0% LLM pesado)."""
        var = variant if variant in CT2_WHISPER_MODELS else self.default_variant
        self.models_dir.mkdir(parents=True, exist_ok=True)

        # 1. Silero VAD + WeSpeaker Voiceprint (~26 MB) [0.00 -> 0.12]
        if self.voiceprint_service:
            ok_vp, msg_vp = self.voiceprint_service.ensure_models_downloaded(
                on_progress=(lambda m, p: progress_cb(m, 0.12 * p)) if progress_cb else None
            )
            if not ok_vp:
                logger.warning(f"Aviso ao baixar modelos auxiliares VAD/Voiceprint: {msg_vp}")

        # 2. Modelo Whisper CT2 INT8 (preferencial) com fallback automático ONNX [0.12 -> 0.92]
        whisper_ok, whisper_msg = self._ensure_ct2_models_ready(
            var, progress_cb=progress_cb, base_pct=0.12, span_pct=0.80
        )
        if not whisper_ok:
            if self._resolve_model_files(var) is not None:
                whisper_ok, whisper_msg = True, f"Usando Whisper {var} ONNX fallback."
            else:
                whisper_ok, whisper_msg = self._ensure_sherpa_models_ready(var, progress_cb=progress_cb)

        if not whisper_ok:
            return False, whisper_msg

        # 3. Denoiser neural GTCRN (~520 KB) [0.92 -> 1.00]
        self.ensure_denoiser_ready(progress_cb=progress_cb, base_pct=0.92)

        if progress_cb:
            progress_cb(f"Modelos Offline ({var} + GTCRN) prontos!", 1.0)
        return True, f"Modelos offline ({var} CT2/ONNX + GTCRN) prontos para uso 100% offline."

    def _ensure_sherpa_models_ready(
        self,
        var: str,
        progress_cb: Optional[Callable[[str, float], None]] = None,
    ) -> Tuple[bool, str]:
        """Fallback legado para download do modelo Whisper INT8 no formato sherpa-onnx."""
        spec = WHISPER_MODELS.get(var, WHISPER_MODELS["small"])
        if self._resolve_model_files(var) is not None:
            return True, f"Modelos Whisper ({var} ONNX) já estão prontos."

        tmp_archive = self.models_dir / f".{spec['dir_name']}.tar.bz2.tmp"
        extract_tmp = self.models_dir / f".extract_{spec['dir_name']}"
        target_dir = self.models_dir / spec["dir_name"]

        try:
            _download_file_parallel(
                spec["url"],
                tmp_archive,
                progress_cb=progress_cb,
                label=f"Baixando Whisper {var} ONNX",
                base_pct=0.12,
                span_pct=0.72,
                num_workers=4,
            )
            if progress_cb:
                progress_cb(f"Extraindo modelo Whisper {var} ONNX...", 0.88)

            if extract_tmp.exists():
                shutil.rmtree(extract_tmp, ignore_errors=True)
            extract_tmp.mkdir(parents=True, exist_ok=True)
            _safe_extract_tar_bz2(tmp_archive, extract_tmp)

            sub_dir = extract_tmp / spec["dir_name"]
            if not sub_dir.exists():
                subdirs = [d for d in extract_tmp.iterdir() if d.is_dir()]
                sub_dir = subdirs[0] if subdirs else extract_tmp

            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
            shutil.move(str(sub_dir), str(target_dir))

            for int8_file in target_dir.rglob("*.int8.onnx"):
                fp32_sibling = int8_file.with_name(int8_file.name.replace(".int8.onnx", ".onnx"))
                if fp32_sibling.exists():
                    fp32_sibling.unlink(missing_ok=True)

            if self._resolve_model_files(var) is None:
                return False, f"Arquivos Whisper inválidos após extração em {target_dir}."
            return True, f"Modelo Whisper ({var} ONNX) baixado e validado."
        except Exception as e:
            logger.exception(f"Falha ao baixar/extrair Whisper {var} ONNX: {e}")
            return False, f"Falha ao preparar Whisper {var}: {e}"
        finally:
            tmp_archive.unlink(missing_ok=True)
            if extract_tmp.exists():
                shutil.rmtree(extract_tmp, ignore_errors=True)

    def ensure_denoiser_ready(
        self,
        progress_cb: Optional[Callable[[str, float], None]] = None,
        base_pct: float = 0.92,
    ) -> Tuple[bool, str]:
        """Garante o modelo neural GTCRN (~520 KB) para redução de ruído e isolamento vocal."""
        if self.denoiser_model_path.exists() and self.denoiser_model_path.stat().st_size > 50_000:
            return True, "Modelo neural de isolamento de voz (GTCRN) pronto."
        try:
            self._ensure_sherpa()
            self.denoiser_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = self.denoiser_model_path.with_suffix(".onnx.tmp")
            if progress_cb:
                progress_cb("Baixando isolador neural de voz (GTCRN ~520 KB)...", base_pct)
            req = urllib.request.Request(DENOISER_MODEL_URL, headers={"User-Agent": "Escutai/2.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                with open(tmp_file, "wb") as f:
                    f.write(resp.read())
            if tmp_file.stat().st_size < 50_000:
                tmp_file.unlink(missing_ok=True)
                return False, "Download incompleto do modelo GTCRN."
            tmp_file.replace(self.denoiser_model_path)
            return True, "Modelo neural GTCRN pronto."
        except Exception as e:
            logger.warning(f"Falha ao baixar modelo GTCRN: {e}")
            return False, f"GTCRN indisponível: {e}"

    def _ensure_denoiser(self):
        with self._denoiser_lock:
            if self._denoiser is not None:
                return self._denoiser
            ok, msg = self.ensure_denoiser_ready()
            if not ok:
                raise RuntimeError(msg)
            so = self._ensure_sherpa()
            threads = min(4, max(2, (os.cpu_count() or 4) // 2))
            cfg = so.OfflineSpeechDenoiserConfig(
                model=so.OfflineSpeechDenoiserModelConfig(
                    gtcrn=so.OfflineSpeechDenoiserGtcrnModelConfig(model=str(self.denoiser_model_path)),
                    num_threads=threads,
                    debug=False,
                    provider="cpu",
                )
            )
            self._denoiser = so.OfflineSpeechDenoiser(cfg)
            return self._denoiser

    def unload_asr_models(self) -> None:
        """Libera Whisper (CT2/ONNX) e GTCRN da memória RAM antes da etapa LLM."""
        with self._lock:
            self._ct2_model = None
            self._recognizer = None
            self._loaded_ct2_variant = None
            self._loaded_variant = None
            self._rolling_prompt_tail = ""
        with self._denoiser_lock:
            self._denoiser = None
        gc.collect()
        logger.info("[MEMÓRIA] Modelos Whisper e GTCRN descarregados da RAM.")

    def denoise_and_isolate_voice(
        self,
        samples_f32: np.ndarray,
        sample_rate: int = TARGET_SAMPLE_RATE,
    ) -> np.ndarray:
        """Executa isolamento neural GTCRN para separar voz humana de ruído/eco de fundo."""
        if len(samples_f32) == 0:
            return samples_f32
        arr = samples_f32.astype(np.float32)
        if sample_rate != TARGET_SAMPLE_RATE:
            pcm_i16 = np.clip(np.round(arr * 32768.0), -32768, 32767).astype(np.int16)
            arr = _to_float32_mono(resample_to_16k(pcm_i16, sample_rate, TARGET_SAMPLE_RATE))

        try:
            denoiser = self._ensure_denoiser()
            max_chunk = 30 * TARGET_SAMPLE_RATE
            parts: List[np.ndarray] = []
            with self._denoiser_lock:
                for pos in range(0, len(arr), max_chunk):
                    sub = arr[pos : pos + max_chunk]
                    if len(sub) < 160:
                        parts.append(sub)
                        continue
                    res = denoiser.run(sub, TARGET_SAMPLE_RATE)
                    parts.append(np.asarray(res.samples, dtype=np.float32))
            return np.concatenate(parts) if len(parts) > 1 else (parts[0] if parts else arr)
        except Exception as e:
            logger.warning(f"Fallback sem denoiser neural GTCRN: {e}")
            return arr

    def _ensure_ct2_recognizer(self, variant: Optional[str] = None):
        var = variant if variant in CT2_WHISPER_MODELS else self.default_variant
        with self._lock:
            if self._ct2_model is not None and self._loaded_ct2_variant == var:
                return self._ct2_model
            model_dir = self._resolve_ct2_model_dir(var)
            if model_dir is None:
                raise RuntimeError(f"Modelo CT2 Whisper '{var}' não encontrado em {self.models_dir}.")
            fw = _import_faster_whisper()
            threads = min(6, max(4, (os.cpu_count() or 4) // 2))
            self._ct2_model = fw.WhisperModel(
                str(model_dir),
                device="cpu",
                compute_type="int8",
                cpu_threads=threads,
                local_files_only=True,
            )
            self._loaded_ct2_variant = var
            logger.info(f"[CT2] Whisper '{var}' carregado (int8, {threads} threads) de {model_dir}.")
            return self._ct2_model

    def _ensure_recognizer(self, variant: Optional[str] = None):
        """Carrega reconhecedor fallback sherpa-onnx caso CT2 não esteja presente."""
        var = variant if variant in WHISPER_MODELS else self.default_variant
        with self._lock:
            if self._recognizer is not None and self._loaded_variant == var:
                return self._recognizer

            files = self._resolve_model_files(var)
            if files is None:
                raise RuntimeError(f"Modelo Whisper ONNX '{var}' não encontrado em {self.models_dir}.")

            so = self._ensure_sherpa()
            enc, dec, tok = files
            threads = min(8, max(4, (os.cpu_count() or 4) - 2))
            self._recognizer = so.OfflineRecognizer.from_whisper(
                encoder=str(enc),
                decoder=str(dec),
                tokens=str(tok),
                language="pt",
                task="transcribe",
                num_threads=threads,
                decoding_method="greedy_search",
            )
            self._loaded_variant = var
            return self._recognizer

    def _transcribe_channel_ct2(
        self,
        merged_turns: List[Tuple[float, float, str, np.ndarray]],
        block_offset_seconds: float,
        variant: Optional[str] = None,
        context_notes: str = "",
        enrolled_names: Optional[List[str]] = None,
        strip_voce_suffix: bool = False,
    ) -> List[Tuple[float, float, str, str]]:
        """Empacota apenas turnos de fala em buffers <=28s (0,25s silêncio entre eles) e alinha palavras via word_timestamps."""
        if not merged_turns:
            return []

        ct2_model = self._ensure_ct2_recognizer(variant)
        # Mantém prompt neutro e sem hotwords para impedir eco de nomes ou alucinações de glossário
        initial_prompt = "Transcrição fiel de reunião de trabalho em português do Brasil, com pontuação clara."
        hotwords_str = None

        sep = np.zeros(int(0.25 * TARGET_SAMPLE_RATE), dtype=np.float32)
        max_buf_samples = int(28.0 * TARGET_SAMPLE_RATE)
        min_turn_samples = int(0.20 * TARGET_SAMPLE_RATE)

        # Monta lotes empacotados de turnos de fala contínua (sem buracos longos de silêncio)
        packed_batches: List[Tuple[List[np.ndarray], List[Tuple[float, float, float, float, str]]]] = []
        cur_arrays: List[np.ndarray] = []
        cur_slots: List[Tuple[float, float, float, float, str]] = []
        cur_samples = 0

        for s_t, e_t, spk, seg_arr in merged_turns:
            if len(seg_arr) < min_turn_samples:
                continue
            clipped = seg_arr[: int(29.0 * TARGET_SAMPLE_RATE)].astype(np.float32)
            need_sep = len(sep) if cur_arrays else 0
            if cur_arrays and (cur_samples + need_sep + len(clipped)) > max_buf_samples:
                packed_batches.append((cur_arrays, cur_slots))
                cur_arrays = []
                cur_slots = []
                cur_samples = 0
                need_sep = 0

            if need_sep:
                cur_arrays.append(sep)
                cur_samples += len(sep)
            b_start = cur_samples / float(TARGET_SAMPLE_RATE)
            cur_arrays.append(clipped)
            cur_samples += len(clipped)
            b_end = cur_samples / float(TARGET_SAMPLE_RATE)
            cur_slots.append((b_start, b_end, s_t, e_t, spk))

        if cur_arrays:
            packed_batches.append((cur_arrays, cur_slots))

        if not packed_batches:
            return []

        events: List[Tuple[float, float, str, str]] = []
        for arr_list, slots in packed_batches:
            pack_f32 = np.concatenate(arr_list) if len(arr_list) > 1 else arr_list[0]
            with self._lock:
                seg_iter, _info = ct2_model.transcribe(
                    pack_f32,
                    language="pt",
                    task="transcribe",
                    beam_size=2,
                    best_of=2,
                    patience=1.0,
                    temperature=[0.0, 0.2],
                    compression_ratio_threshold=2.1,
                    log_prob_threshold=-1.0,
                    no_speech_threshold=0.6,
                    condition_on_previous_text=False,
                    word_timestamps=True,
                    initial_prompt=initial_prompt,
                    hotwords=hotwords_str,
                    repetition_penalty=1.20,
                    no_repeat_ngram_size=3,
                    vad_filter=False,
                )
                raw_segments = list(seg_iter)

            slot_tokens: List[List[str]] = [[] for _ in slots]
            buf_total_t = len(pack_f32) / float(TARGET_SAMPLE_RATE)

            def _assign_span(w_s: float, w_e: float, w_tok: str) -> None:
                if not w_tok:
                    return
                w_mid = 0.5 * (w_s + w_e)
                if w_mid > buf_total_t + 0.6 or w_mid < -0.5:
                    return
                best_k = 0
                best_dist = float("inf")
                for k_idx, (bs, be, _os, _oe, _spk) in enumerate(slots):
                    if bs <= w_mid <= be:
                        slot_tokens[k_idx].append(w_tok)
                        return
                    dist = max(bs - w_mid, w_mid - be, 0.0)
                    if dist < best_dist:
                        best_dist = dist
                        best_k = k_idx
                if best_dist <= 0.65:
                    slot_tokens[best_k].append(w_tok)

            for seg in raw_segments:
                words = getattr(seg, "words", None) or []
                if words:
                    for w in words:
                        w_txt = str(getattr(w, "word", "") or "")
                        ws = float(getattr(w, "start", seg.start))
                        we = float(getattr(w, "end", max(ws, getattr(seg, "end", ws))))
                        _assign_span(ws, we, w_txt)
                else:
                    s_txt = str(getattr(seg, "text", "") or "").strip()
                    if s_txt:
                        _assign_span(float(seg.start), float(seg.end), " " + s_txt)

            for (_bs, _be, orig_s, orig_e, spk), toks in zip(slots, slot_tokens):
                if not toks:
                    continue
                raw_joined = "".join(toks).strip()
                cleaned = _clean_whisper_text(raw_joined)
                if not cleaned:
                    continue
                fixed_txt = _apply_glossary_and_phonetic_fixes(
                    cleaned, context_notes=context_notes, enrolled_names=enrolled_names
                )
                if not fixed_txt:
                    continue
                final_spk = "Willian" if (strip_voce_suffix and spk.endswith("(Você)")) else spk
                abs_s = block_offset_seconds + orig_s
                abs_e = block_offset_seconds + orig_e
                if events and events[-1][2] == final_spk and (abs_s - events[-1][1]) <= 2.0:
                    p_s, p_e, p_spk, p_txt = events[-1]
                    events[-1] = (p_s, max(p_e, abs_e), p_spk, f"{p_txt} {fixed_txt}".strip())
                else:
                    events.append((abs_s, abs_e, final_spk, fixed_txt))

        return events

    def _transcribe_turns_ct2(
        self,
        chunks: List[np.ndarray],
        variant: Optional[str] = None,
        context_notes: str = "",
        enrolled_names: Optional[List[str]] = None,
    ) -> List[str]:
        """Decodifica turnos via faster-whisper CTranslate2 (beam_size=5 + initial_prompt + hotwords)."""
        if not chunks:
            return []
        ct2_model = self._ensure_ct2_recognizer(variant)
        initial_prompt = "Transcrição fiel de reunião de trabalho em português do Brasil, com pontuação clara."
        hotwords_str = None

        min_len = int(0.25 * TARGET_SAMPLE_RATE)
        max_len = int(29.5 * TARGET_SAMPLE_RATE)
        outputs = [""] * len(chunks)

        with self._lock:
            for idx, wav_arr in enumerate(chunks):
                if len(wav_arr) < min_len:
                    continue
                audio_f32 = wav_arr[:max_len].astype(np.float32)

                segments, _info = ct2_model.transcribe(
                    audio_f32,
                    language="pt",
                    task="transcribe",
                    beam_size=2,
                    best_of=2,
                    patience=1.0,
                    temperature=[0.0, 0.2],
                    compression_ratio_threshold=2.1,
                    log_prob_threshold=-1.0,
                    no_speech_threshold=0.6,
                    condition_on_previous_text=False,
                    initial_prompt=initial_prompt,
                    hotwords=hotwords_str,
                    repetition_penalty=1.20,
                    no_repeat_ngram_size=3,
                    vad_filter=False,
                )
                seg_texts = [getattr(s, "text", "").strip() for s in segments if getattr(s, "text", "").strip()]
                outputs[idx] = _clean_whisper_text(" ".join(seg_texts))

        return outputs

    def _transcribe_samples(self, samples_f32: np.ndarray, variant: Optional[str] = None) -> str:
        results = self._transcribe_batch_samples([samples_f32], variant=variant)
        return results[0] if results else ""

    def _transcribe_batch_samples(
        self,
        chunks: List[np.ndarray],
        variant: Optional[str] = None,
        context_notes: str = "",
        enrolled_names: Optional[List[str]] = None,
    ) -> List[str]:
        """Decodifica turnos via CTranslate2 (`beam_size=5`) ou fallback `sherpa-onnx` (`decode_streams`)."""
        if not chunks:
            return []
        var = variant if variant in CT2_WHISPER_MODELS else self.default_variant
        if self._resolve_ct2_model_dir(var) is not None and self._can_import_faster_whisper():
            try:
                return self._transcribe_turns_ct2(
                    chunks,
                    variant=var,
                    context_notes=context_notes,
                    enrolled_names=enrolled_names,
                )
            except Exception as e:
                logger.warning(f"Falha no decoder CT2 Whisper ({var}), tentando fallback ONNX: {e}")

        rec = self._ensure_recognizer(var)
        min_len = int(0.25 * TARGET_SAMPLE_RATE)
        max_len = int(29.5 * TARGET_SAMPLE_RATE)
        outputs = [""] * len(chunks)

        valid_entries: List[Tuple[int, np.ndarray]] = []
        for idx, ch in enumerate(chunks):
            if len(ch) >= min_len:
                valid_entries.append((idx, ch[:max_len].astype(np.float32)))

        if not valid_entries:
            return outputs

        batch_size = 6
        with self._lock:
            for b_start in range(0, len(valid_entries), batch_size):
                batch = valid_entries[b_start : b_start + batch_size]
                streams = []
                for _orig_idx, wav_arr in batch:
                    s = rec.create_stream()
                    s.accept_waveform(TARGET_SAMPLE_RATE, wav_arr)
                    streams.append(s)
                if len(streams) > 1 and hasattr(rec, "decode_streams"):
                    rec.decode_streams(streams)
                else:
                    for s in streams:
                        rec.decode_stream(s)
                for (orig_idx, _), s in zip(batch, streams):
                    raw_txt = getattr(s.result, "text", "") or ""
                    outputs[orig_idx] = _clean_whisper_text(raw_txt)

        return outputs

    def _segment_channel(self, mono_f32: np.ndarray, sample_rate: int) -> List[Tuple[float, float, np.ndarray]]:
        """Extrai segmentos de fala (~<=15s) via Silero VAD ou janelas fixas de fallback se VAD indisponível."""
        if sample_rate != TARGET_SAMPLE_RATE:
            pcm_i16 = np.clip(mono_f32 * 32767.0, -32768, 32767).astype(np.int16)
            mono_f32 = _to_float32_mono(resample_to_16k(pcm_i16, sample_rate, TARGET_SAMPLE_RATE))

        if len(mono_f32) == 0:
            return []

        if self.voiceprint_service:
            try:
                segs = self.voiceprint_service.extract_speech_segments(mono_f32, TARGET_SAMPLE_RATE)
                if segs:
                    return segs
            except Exception as e:
                logger.debug(f"VAD fallback por janelas: {e}")

        rms = float(np.sqrt(np.mean(mono_f32 * mono_f32)))
        if rms < 0.005:
            return []

        win = 18 * TARGET_SAMPLE_RATE
        out: List[Tuple[float, float, np.ndarray]] = []
        for pos in range(0, len(mono_f32), win):
            sub = mono_f32[pos : pos + win]
            if len(sub) >= int(0.4 * TARGET_SAMPLE_RATE):
                out.append((pos / float(TARGET_SAMPLE_RATE), (pos + len(sub)) / float(TARGET_SAMPLE_RATE), sub))
        return out

    def _identify_speaker(
        self,
        seg_f32: np.ndarray,
        unknown_clusters: List[Tuple[str, np.ndarray, int]],
    ) -> str:
        if not self.voiceprint_service:
            return "Participante 1"
        try:
            return self.voiceprint_service.identify_speaker_segment(
                seg_f32, TARGET_SAMPLE_RATE, unknown_clusters=unknown_clusters
            )
        except Exception:
            return "Participante 1"

    @staticmethod
    def _smooth_short_speaker_islands(
        diarized_segs: List[Tuple[float, float, str, np.ndarray]],
        max_island_sec: float = 2.5,
        max_gap_sec: float = 2.0,
    ) -> List[Tuple[float, float, str, np.ndarray]]:
        """Suaviza pontes curtas de 1-2 segmentos (A -> [B, C] -> A) e absorve micro-fragmentos <=1,1s."""
        if len(diarized_segs) < 2:
            return diarized_segs
        smoothed = list(diarized_segs)

        # Passo 1: Pontes curtas de 1 ou 2 turnos entre retornos do mesmo locutor (A -> [B] -> A / A -> [B, C] -> A)
        if len(smoothed) >= 3:
            for width in (1, 2):
                i = 1
                while i + width < len(smoothed):
                    left_s, left_e, left_spk, _ = smoothed[i - 1]
                    right_s, _re, right_spk, _ = smoothed[i + width]
                    if left_spk == right_spk and not left_spk.startswith("Possível sobreposição"):
                        mid = smoothed[i : i + width]
                        span_dur = mid[-1][1] - mid[0][0]
                        all_ok = (
                            span_dur <= 3.4
                            and all(
                                (m[1] - m[0]) <= max_island_sec
                                and m[2] != left_spk
                                and not m[2].startswith("Possível sobreposição")
                                for m in mid
                            )
                            and (mid[0][0] - left_e) <= max_gap_sec
                            and (right_s - mid[-1][1]) <= max_gap_sec
                            and (width == 1 or (mid[1][0] - mid[0][1]) <= max_gap_sec)
                        )
                        if all_ok:
                            for offset in range(width):
                                ms, me, _mspk, marr = smoothed[i + offset]
                                smoothed[i + offset] = (ms, me, left_spk, marr)
                    i += 1

        # Passo 2: Absorve fragmentos ultra-curtos (<= 1,1s) adjacentes a turnos longos (>= 2,8s)
        final_segs = list(smoothed)
        for idx, (c_s, c_e, c_spk, c_arr) in enumerate(final_segs):
            if (c_e - c_s) > 1.1 or c_spk.startswith("Possível sobreposição"):
                continue
            cand_left = None
            cand_right = None
            if idx > 0:
                p_s, p_e, p_spk, _ = final_segs[idx - 1]
                if not p_spk.startswith("Possível sobreposição") and (c_s - p_e) <= 0.6 and (p_e - p_s) >= 2.8:
                    cand_left = (c_s - p_e, p_e - p_s, p_spk)
            if idx + 1 < len(final_segs):
                n_s, n_e, n_spk, _ = final_segs[idx + 1]
                if not n_spk.startswith("Possível sobreposição") and (n_s - c_e) <= 0.6 and (n_e - n_s) >= 2.8:
                    cand_right = (n_s - c_e, n_e - n_s, n_spk)
            chosen_spk = None
            if cand_left and cand_right:
                chosen_spk = cand_left[2] if cand_left[0] <= cand_right[0] else cand_right[2]
            elif cand_left:
                chosen_spk = cand_left[2]
            elif cand_right:
                chosen_spk = cand_right[2]
            if chosen_spk and chosen_spk != c_spk:
                final_segs[idx] = (c_s, c_e, chosen_spk, c_arr)

        return final_segs

    @staticmethod
    def _merge_same_speaker_turns(
        diarized_segs: List[Tuple[float, float, str, np.ndarray]],
        max_gap_sec: float = 2.5,
        max_turn_sec: float = 24.0,
    ) -> List[Tuple[float, float, str, np.ndarray]]:
        """Mescla trechos consecutivos do mesmo locutor para decodificação rápida e maior contexto."""
        if not diarized_segs:
            return []
        pad = np.zeros(int(0.14 * TARGET_SAMPLE_RATE), dtype=np.float32)
        merged: List[Tuple[float, float, str, np.ndarray]] = []
        for s_t, e_t, spk, seg in diarized_segs:
            if (
                merged
                and merged[-1][2] == spk
                and (s_t - merged[-1][1]) <= max_gap_sec
                and (e_t - merged[-1][0]) <= max_turn_sec
            ):
                p_s, _p_e, p_spk, p_seg = merged[-1]
                merged[-1] = (p_s, max(_p_e, e_t), p_spk, np.concatenate([p_seg, pad, seg]))
            else:
                merged.append((s_t, e_t, spk, seg))
        return merged

    def _process_channel_turns(
        self,
        chan_f32: np.ndarray,
        sr: int,
        block_offset_seconds: float,
        variant: Optional[str],
        clusters: List[Tuple[str, np.ndarray, int]],
        fixed_speaker: Optional[str] = None,
        context_notes: str = "",
        enrolled_names: Optional[List[str]] = None,
        isolate_voice: bool = False,
        clarify_audio: bool = True,
        enhancement_mode: str = "clarify_room",
        strip_voce_suffix: bool = False,
    ) -> List[Tuple[float, float, str, str]]:
        """Pipeline dual-branch: VAD+Voiceprint no timbre natural -> colarinho + restauração no ASR."""
        if len(chan_f32) == 0:
            return []

        raw_f32 = chan_f32
        if sr != TARGET_SAMPLE_RATE:
            pcm_i16 = np.clip(np.round(raw_f32 * 32768.0), -32768, 32767).astype(np.int16)
            raw_f32 = _to_float32_mono(resample_to_16k(pcm_i16, sr, TARGET_SAMPLE_RATE))

        # Ramo A (timbre natural 16 kHz): VAD + Voiceprint preservam formantes originais dos perfis
        vad_segs = self._segment_channel(raw_f32, TARGET_SAMPLE_RATE)
        if not vad_segs:
            return []

        if fixed_speaker is not None:
            diarized = [(s_t, e_t, fixed_speaker, seg) for s_t, e_t, seg in vad_segs]
        elif self.voiceprint_service:
            diarized = self.voiceprint_service.diarize_speech_segments(
                vad_segs, TARGET_SAMPLE_RATE, unknown_clusters=clusters
            )
            diarized = self._smooth_short_speaker_islands(diarized)
        else:
            diarized = [(s_t, e_t, "Participante 1", seg) for s_t, e_t, seg in vad_segs]

        # Ramo B (ASR restaurado): isolamento neural GTCRN e/ou EQ vocal apenas para decodificação Whisper
        eff_mode = (
            enhancement_mode
            if enhancement_mode in ("none", "light", "clarify_room", "denoise_and_clarify", "neural_voice_isolation")
            else ("clarify_room" if clarify_audio else "none")
        )
        asr_f32 = raw_f32
        if isolate_voice or eff_mode == "neural_voice_isolation":
            asr_f32 = self.denoise_and_isolate_voice(asr_f32, TARGET_SAMPLE_RATE)
        if eff_mode != "none":
            pcm_i16 = np.clip(np.round(asr_f32 * 32768.0), -32768, 32767).astype(np.int16)
            asr_f32 = _to_float32_mono(enhance_speech_audio(pcm_i16, TARGET_SAMPLE_RATE, mode=eff_mode))

        # Recorte com colarinho de 0,24s (limitado a 0,08s sobre vizinhos) para evitar corte de sílaba final
        n_asr = len(asr_f32)
        max_dur_t = n_asr / float(TARGET_SAMPLE_RATE)
        diarized_asr: List[Tuple[float, float, str, np.ndarray]] = []
        for idx, (s_t, e_t, spk, raw_seg) in enumerate(diarized):
            prev_e = diarized[idx - 1][1] if idx > 0 else 0.0
            next_s = diarized[idx + 1][0] if idx + 1 < len(diarized) else max_dur_t
            min_allowed_s = (prev_e - 0.08) if idx > 0 else 0.0
            max_allowed_e = (next_s + 0.08) if idx + 1 < len(diarized) else max_dur_t
            eff_s_t = max(0.0, min(s_t, max(s_t - 0.24, min_allowed_s)))
            eff_e_t = min(max_dur_t, max(e_t, min(e_t + 0.24, max_allowed_e)))
            i0 = max(0, int(round(eff_s_t * TARGET_SAMPLE_RATE)))
            i1 = min(n_asr, int(round(eff_e_t * TARGET_SAMPLE_RATE)))
            asr_seg = asr_f32[i0:i1] if i1 > i0 else raw_seg
            diarized_asr.append((s_t, e_t, spk, asr_seg))

        merged = self._merge_same_speaker_turns(diarized_asr)

        # Via preferencial rápida: empacota apenas turnos de fala (<=28s) no CT2 + word_timestamps
        var = variant if variant in CT2_WHISPER_MODELS else self.default_variant
        if self._resolve_ct2_model_dir(var) is not None and self._can_import_faster_whisper():
            try:
                return self._transcribe_channel_ct2(
                    merged,
                    block_offset_seconds,
                    variant=var,
                    context_notes=context_notes,
                    enrolled_names=enrolled_names,
                    strip_voce_suffix=strip_voce_suffix,
                )
            except Exception as e:
                logger.warning(f"Falha na decodificação empacotada CT2 ({var}), usando fallback ONNX por turno: {e}")

        texts = self._transcribe_batch_samples(
            [m[3] for m in merged],
            variant=variant,
            context_notes=context_notes,
            enrolled_names=enrolled_names,
        )

        events: List[Tuple[float, float, str, str]] = []
        for (s_t, e_t, spk, _seg), raw_txt in zip(merged, texts):
            if not raw_txt:
                continue
            fixed_txt = _apply_glossary_and_phonetic_fixes(
                raw_txt, context_notes=context_notes, enrolled_names=enrolled_names
            )
            if not fixed_txt:
                continue
            final_spk = spk
            if strip_voce_suffix and final_spk.endswith("(Você)"):
                final_spk = "Willian"
            events.append((block_offset_seconds + s_t, block_offset_seconds + e_t, final_spk, fixed_txt))
        return events

    def transcribe_wav_block(
        self,
        wav_path: Path,
        block_offset_seconds: float = 0.0,
        variant: Optional[str] = None,
        session_unknown_clusters: Optional[List[Tuple[str, np.ndarray, int]]] = None,
        context_notes: str = "",
        enrolled_names: Optional[List[str]] = None,
        isolate_voice: bool = False,
        clarify_audio: bool = True,
        enhancement_mode: str = "clarify_room",
        mic_owner_filter_enabled: bool = False,
        mic_owner_profile_name: str = "",
    ) -> str:
        """Transcreve um bloco WAV (1ch Mono ou 2ch Estéreo L/R) 100% offline com diarização."""
        wav_path = Path(wav_path)
        with wave.open(str(wav_path), "rb") as wf:
            nchannels = int(wf.getnchannels())
            sampwidth = int(wf.getsampwidth())
            sr = int(wf.getframerate())
            frames = int(wf.getnframes())
            if sampwidth != 2 or frames == 0 or sr <= 0:
                return "*[Silêncio / Sem fala identificada no período]*"
            raw = wf.readframes(frames)

        arr = np.frombuffer(raw, dtype=np.int16)
        clusters = session_unknown_clusters if session_unknown_clusters is not None else []
        events: List[Tuple[float, float, str, str]] = []

        if nchannels >= 2:
            stereo = arr.reshape(-1, nchannels)
            left_f = _to_float32_mono(stereo[:, 0])
            right_f = _to_float32_mono(stereo[:, 1])

            if mic_owner_filter_enabled and self.voiceprint_service and self.voiceprint_service.is_ready_for_matching():
                owner_name = self.voiceprint_service.resolve_owner_profile_name(mic_owner_profile_name)
                if owner_name:
                    right_f, _ = self.voiceprint_service.filter_channel_to_speaker_samples(
                        right_f, sr, owner_name=owner_name
                    )

            events.extend(
                self._process_channel_turns(
                    right_f, sr, block_offset_seconds, variant, clusters,
                    fixed_speaker="Willian (Você)", context_notes=context_notes,
                    enrolled_names=enrolled_names, isolate_voice=isolate_voice,
                    clarify_audio=clarify_audio, enhancement_mode=enhancement_mode,
                )
            )
            events.extend(
                self._process_channel_turns(
                    left_f, sr, block_offset_seconds, variant, clusters,
                    fixed_speaker=None, context_notes=context_notes,
                    enrolled_names=enrolled_names, isolate_voice=isolate_voice,
                    clarify_audio=clarify_audio, enhancement_mode=enhancement_mode,
                )
            )
        else:
            mono_f = _to_float32_mono(arr)
            events.extend(
                self._process_channel_turns(
                    mono_f, sr, block_offset_seconds, variant, clusters,
                    fixed_speaker=None, context_notes=context_notes,
                    enrolled_names=enrolled_names, isolate_voice=isolate_voice,
                    clarify_audio=clarify_audio, enhancement_mode=enhancement_mode,
                    strip_voce_suffix=True,
                )
            )

        if not events:
            return "*[Silêncio / Sem fala identificada no período]*"

        events.sort(key=lambda item: item[0])

        lines: List[str] = []
        idx = 0
        while idx < len(events):
            s_a, e_a, spk_a, txt_a = events[idx]
            if idx + 1 < len(events):
                s_b, e_b, spk_b, txt_b = events[idx + 1]
                overlap_sec = min(e_a, e_b) - max(s_a, s_b)
                if spk_a != spk_b and (overlap_sec >= 0.35 or abs(s_a - s_b) <= 0.6):
                    lines.append(f"[{_format_mmss(s_a)}] ⚡ [Falas Sobrepostas / Interrupção]:")
                    lines.append(f"  - **{spk_a}**: {txt_a}")
                    lines.append(f"  - **{spk_b}**: {txt_b}")
                    idx += 2
                    continue

            lines.append(f"[{_format_mmss(s_a)}] **{spk_a}**: {txt_a}")
            idx += 1

        return "\n".join(lines)

    # ---------------- ESTÁGIO 2: SÍNTESE OFFLINE INSTANTÂNEA / OLLAMA OPCIONAL ----------------
    def stop_llm_server(self) -> None:
        """No-op seguro mantido para compatibilidade após remoção do LLM embutido."""
        gc.collect()

    def _chat_complete_local(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.15,
        max_tokens: int = 1600,
        timeout: int = 180,
        preferred_ollama_model: str = "",
    ) -> Tuple[Optional[str], str]:
        """Executa inferência local exclusivamente se um servidor Ollama externo estiver ativo."""
        ok_ollama, models = list_ollama_models()
        if not (ok_ollama and models):
            return None, "unavailable"

        chosen = preferred_ollama_model.strip()
        if not chosen or chosen not in models:
            chosen = next(
                (
                    m for m in models
                    if any(k in m.lower() for k in ("qwen", "llama", "gemma", "mistral", "phi"))
                ),
                models[0],
            )
        try:
            payload = json.dumps(
                {
                    "model": chosen,
                    "prompt": f"{system_prompt}\n\n{user_prompt}",
                    "stream": False,
                    "options": {"temperature": temperature, "num_predict": max_tokens},
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                "http://127.0.0.1:11434/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
            txt = str(body.get("response", "")).strip()
            if txt:
                return txt, f"Ollama Local ({chosen})"
        except Exception as e:
            logger.warning(f"Falha no Ollama ({chosen}): {e}")
        return None, "unavailable"

    def refine_offline_transcript(
        self,
        raw_transcript: str,
        title: str = "",
        context_notes: str = "",
        enrolled_names: Optional[List[str]] = None,
        preferred_ollama_model: str = "",
    ) -> str:
        """Refina erros fonéticos via Ollama externo se ativo; caso contrário retorna imediatamente."""
        if not raw_transcript or not raw_transcript.strip():
            return raw_transcript

        ok_ollama, ol_models = list_ollama_models()
        if not (ok_ollama and ol_models):
            return raw_transcript

        self.unload_asr_models()
        turn_lines = [ln.strip() for ln in raw_transcript.splitlines() if _TURN_RE.match(ln.strip())]
        if not turn_lines:
            return raw_transcript

        sample_turns = "\n".join(turn_lines)[:5500]
        vocab = _extract_glossary_terms(context_notes, enrolled_names)
        names_txt = ", ".join(enrolled_names or []) or "Não especificados"
        glossary_txt = ", ".join(vocab[:30])

        sys_prompt = (
            "Você é um revisor fonético de transcrições em Português do Brasil.\n"
            "Identifique apenas erros evidentes de reconhecimento de voz (nomes distorcidos ou termos técnicos/corporativos "
            "trocados pelo som) no texto abaixo e responda EXCLUSIVAMENTE com um JSON válido no formato:\n"
            "{\"correcoes\": {\"trecho errado exato\": \"trecho corrigido\"}}\n"
            "Inclua no máximo 10 correções de alta confiança. NÃO inclua timestamps `[MM:SS]`, `**` ou comentários."
        )
        user_prompt = (
            f"Reunião: {title or 'Alinhamento'}\n"
            f"Participantes cadastrados: {names_txt}\n"
            f"Glossário prioritário: {glossary_txt}\n"
            f"Notas de contexto: {context_notes or 'Nenhuma'}\n\n"
            f"Falas transcritas:\n{sample_turns}"
        )

        out_txt, _src = self._chat_complete_local(
            sys_prompt,
            user_prompt,
            temperature=0.1,
            max_tokens=220,
            timeout=90,
            preferred_ollama_model=preferred_ollama_model,
        )
        if not out_txt:
            return raw_transcript

        cleaned_json_txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", out_txt.strip()).strip()
        m_obj = re.search(r"\{.*\}", cleaned_json_txt, flags=re.DOTALL)
        if not m_obj:
            return raw_transcript

        try:
            parsed = json.loads(m_obj.group(0))
        except Exception:
            return raw_transcript

        raw_map = parsed.get("correcoes") if isinstance(parsed, dict) else None
        if not isinstance(raw_map, dict) or not raw_map:
            return raw_transcript

        valid_pairs: List[Tuple[re.Pattern, str]] = []
        for wrong, right in raw_map.items():
            if not isinstance(wrong, str) or not isinstance(right, str):
                continue
            w_clean = wrong.strip()
            r_clean = right.strip()
            if not (3 <= len(w_clean) <= 45 and 3 <= len(r_clean) <= 45):
                continue
            if w_clean.lower() == r_clean.lower():
                continue
            if any(ch in w_clean or ch in r_clean for ch in ("[", "]", "**", "\n", "\r")):
                continue
            pat = re.compile(rf"(?<!\w){re.escape(w_clean)}(?!\w)", flags=re.IGNORECASE)
            valid_pairs.append((pat, r_clean))
            if len(valid_pairs) >= 10:
                break

        if not valid_pairs:
            return raw_transcript

        new_lines: List[str] = []
        changed = False
        for line in raw_transcript.splitlines():
            m_turn = re.match(r"^(\[\d{1,2}:\d{2}(?::\d{2})?\]\s+(?:⚡\s*)?\*\*[^*]+\*\*:\s*)(.*)$", line)
            if m_turn:
                prefix, spoken = m_turn.group(1), m_turn.group(2)
                updated_spoken = spoken
                for pat, repl in valid_pairs:
                    updated_spoken = pat.sub(repl, updated_spoken)
                if updated_spoken != spoken:
                    changed = True
                new_lines.append(f"{prefix}{updated_spoken}")
            else:
                new_lines.append(line)

        if not changed:
            return raw_transcript
        trailing_nl = "\n" if raw_transcript.endswith("\n") else ""
        return "\n".join(new_lines) + trailing_nl

    @staticmethod
    def _compact_transcript_for_context(transcript_md: str, max_chars: int = 6000) -> str:
        """Extrai somente linhas limpas de diálogo ([MM:SS] **Locutor**: texto) até o limite de contexto."""
        body = (transcript_md or "").strip()
        if body.lstrip().startswith("# Transcrição") and "\n---\n" in body:
            split_idx = body.find("\n---\n")
            body = body[split_idx + len("\n---\n") :]
        turn_lines = [ln.strip() for ln in body.splitlines() if _TURN_RE.match(ln.strip())]
        joined = "\n".join(turn_lines)
        if len(joined) <= max_chars:
            return joined
        step = max(2, len(turn_lines) // 65)
        sampled = [ln for idx, ln in enumerate(turn_lines) if idx % step == 0]
        return "\n".join(sampled)[:max_chars]

    def generate_offline_minutes(
        self,
        title: str,
        date_str: str,
        duration_str: str,
        transcript_md: str,
        minutes_prompt_template: str,
        preferred_ollama_model: str = "",
    ) -> Tuple[str, str]:
        """Gera Ata Executiva via Ollama (se ativo) ou Síntese Local Estruturada instantânea (< 0.05s)."""
        self.unload_asr_models()
        ok_ollama, ol_models = list_ollama_models()
        if ok_ollama and ol_models:
            try:
                compact_tr = self._compact_transcript_for_context(transcript_md, max_chars=8000)
                speakers_seen: List[str] = []
                for ln in compact_tr.splitlines():
                    m_t = _TURN_RE.match(ln.strip())
                    if m_t:
                        spk = m_t.group(2).strip()
                        if spk and spk not in speakers_seen:
                            speakers_seen.append(spk)
                speakers_str = ", ".join(speakers_seen) if speakers_seen else "Não identificados"
                header = (
                    f"# Ata Executiva da Reunião: {title}\n"
                    f"**Data:** {date_str}\n"
                    f"**Duração Total:** {duration_str}\n"
                    f"**Participantes Identificados:** {speakers_str}\n\n"
                    "---\n\n"
                )
                prompt = minutes_prompt_template.format(
                    title=title,
                    date_str=date_str,
                    duration_str=duration_str,
                    full_transcript=compact_tr,
                )
                txt, backend = self._chat_complete_local(
                    "Você é um secretário executivo sênior redigindo uma Ata Executiva completa em Português do Brasil.",
                    prompt,
                    temperature=0.20,
                    max_tokens=1600,
                    timeout=180,
                    preferred_ollama_model=preferred_ollama_model,
                )
                if txt and len(txt.strip()) > 120:
                    cleaned = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", txt.strip()).strip()
                    if cleaned.startswith("# Ata"):
                        return cleaned + "\n", backend
                    return header + cleaned + "\n", backend
            except Exception as e:
                logger.warning(f"Falha ao gerar Ata via Ollama externo: {e}")

        return (
            self._synthesize_deterministic_minutes(title, date_str, duration_str, transcript_md),
            "Síntese Local Estruturada",
        )

    @staticmethod
    def _is_substantive_turn(text: str) -> bool:
        """Retorna True se a fala carrega conteúdo informativo além de interjeições curtas."""
        clean = re.sub(r"[^\wÀ-ÿ\s]", " ", (text or "").lower()).strip()
        words = [w for w in clean.split() if w]
        if not words:
            return False
        if len(words) <= 3 and (" ".join(words) in _BACKCHANNEL_SET or all(w in _BACKCHANNEL_SET for w in words)):
            return False
        return len(text.strip()) >= 28 or len(words) >= 6

    @staticmethod
    def _synthesize_deterministic_minutes(title: str, date_str: str, duration_str: str, transcript_md: str) -> str:
        """Gera Ata Executiva encorpada em 5 seções instantaneamente (< 0.05s, 0 MB RAM)."""
        body = (transcript_md or "").strip()
        if body.lstrip().startswith("# Transcrição") and "\n---\n" in body:
            body = body[body.find("\n---\n") + len("\n---\n") :]

        speaker_turns: Counter = Counter()
        speaker_words: Counter = Counter()
        speaker_entries: Dict[str, List[Tuple[str, str]]] = {}
        blocks: List[Dict[str, Any]] = []
        current_block: Dict[str, Any] = {
            "title": "Bloco Principal",
            "speakers": [],
            "entries": [],
        }

        overlaps_count = 0
        current_overlap_ts: Optional[str] = None
        decisions: List[str] = []
        pains: List[str] = []
        questions: List[str] = []

        def _is_duplicate_line(cand: str, existing_list: List[str]) -> bool:
            cand_low = cand.lower()
            for ex in existing_list:
                if SequenceMatcher(None, cand_low, ex.lower()).ratio() >= 0.72:
                    return True
            return False

        def _record_turn(ts: str, spk: str, txt: str) -> None:
            speaker_turns[spk] += 1
            wc = len(txt.split())
            speaker_words[spk] += wc
            if spk not in current_block["speakers"]:
                current_block["speakers"].append(spk)
            speaker_entries.setdefault(spk, []).append((ts, txt))
            current_block["entries"].append((ts, spk, txt))

            low = txt.lower()
            if OfflineAsrService._is_substantive_turn(txt):
                if any(cue in low for cue in _DECISION_CUES) and len(decisions) < 14:
                    item = f"- `[{ts}]` **{spk}:** {txt}"
                    if not _is_duplicate_line(txt, decisions):
                        decisions.append(item)
                if any(cue in low for cue in _PAIN_CUES) and len(pains) < 10:
                    item = f"- `[{ts}]` **{spk}:** {txt}"
                    if not _is_duplicate_line(txt, pains):
                        pains.append(item)
            if "?" in txt and len(txt.strip()) >= 14 and len(questions) < 12:
                item = f"- `[{ts}]` **{spk}:** {txt}"
                if not _is_duplicate_line(txt, questions):
                    questions.append(item)

        for raw in body.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("### Bloco"):
                if current_block["entries"]:
                    blocks.append(current_block)
                current_block = {
                    "title": line.lstrip("# ").strip(),
                    "speakers": [],
                    "entries": [],
                }
                current_overlap_ts = None
                continue

            m_ov_head = re.match(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*⚡", line)
            if m_ov_head:
                overlaps_count += 1
                current_overlap_ts = m_ov_head.group(1)
                continue

            if current_overlap_ts:
                m_ov = _OVERLAP_TURN_RE.match(line)
                if m_ov:
                    _record_turn(current_overlap_ts, m_ov.group(1).strip(), m_ov.group(2).strip())
                    continue
                current_overlap_ts = None

            m = _TURN_RE.match(line)
            if m:
                _record_turn(m.group(1), m.group(2).strip(), m.group(3).strip())

        if current_block["entries"]:
            blocks.append(current_block)

        total_turns = sum(speaker_turns.values())
        total_words = max(1, sum(speaker_words.values()))
        speakers_list = ", ".join(speaker_turns.keys()) if speaker_turns else "Não identificados"

        # Extrai termos temáticos frequentes (>= 5 letras, excluindo stopwords comuns PT-BR)
        _pt_stop = {
            "porque", "quando", "então", "entao", "sobre", "agora", "muito", "muita", "muitos",
            "tudo", "todo", "toda", "todos", "todas", "isso", "isto", "aquilo", "aqui", "onde",
            "como", "mais", "menos", "também", "tambem", "assim", "fazer", "feito", "fazendo",
            "pode", "podem", "poder", "tenho", "temos", "tinha", "estava", "estão", "estao",
            "estar", "vamos", "gente", "coisa", "coisas", "parte", "partes", "forma", "jeito",
            "nesse", "nessa", "desse", "dessa", "pelo", "pela", "para", "como", "ainda", "sempre",
            "antes", "depois", "outro", "outra", "outros", "algum", "alguma", "alguns", "entre",
            "mesmo", "mesma", "acho", "acha", "tipo", "entendeu", "sabe", "falar", "falou",
            "dizer", "disse", "olhar", "certo", "verdade", "exemplo", "melhor", "maior", "ponto",
            "queria", "quero", "precisa", "precisamos", "semana", "reunião", "reuniao", "bloco",
        }
        token_freq: Counter = Counter()
        for spk_list in speaker_entries.values():
            for _ts, txt in spk_list:
                for tok in re.findall(r"[A-Za-zÀ-ÿ]{5,}", txt):
                    low_t = tok.lower()
                    if low_t not in _pt_stop:
                        token_freq[low_t] += 1
        top_themes = [w.capitalize() for w, c in token_freq.most_common(8) if c >= 2]
        themes_str = ", ".join(top_themes) if top_themes else "Alinhamento operacional e direcionamentos da equipe"

        out: List[str] = [
            f"# Ata Executiva da Reunião: {title}",
            f"**Data:** {date_str}",
            f"**Duração Total:** {duration_str}",
            "**Modo de Processamento:** Transcrição Offline (Whisper Turbo CT2 INT8 + GTCRN) • Síntese Estruturada Local",
            f"**Participantes Identificados:** {speakers_list}",
            "",
            "---",
            "",
            "## 1. Resumo Executivo e Métricas da Sessão",
            (
                f"A reunião **{title}** teve duração de **{duration_str}**, organizada em **{max(1, len(blocks))} bloco(s)** "
                f"com **{total_turns} intervenções** (~{total_words} palavras transcritas)"
                + (f" e **{overlaps_count} momento(s) de fala simultânea**" if overlaps_count else "")
                + f". Os temas de maior recorrência giraram em torno de: **{themes_str}**."
            ),
            "",
        ]
        if speaker_turns:
            out.append("**Distribuição de participação:**")
            for spk, words in speaker_words.most_common():
                pct = round((words / total_words) * 100)
                out.append(f"- **{spk}**: {speaker_turns[spk]} falas (~{words} palavras • {pct}% da reunião)")
            out.append("")

        out.append("## 2. Principais Contribuições por Participante")
        if speaker_entries:
            for spk, _words in speaker_words.most_common():
                entries = speaker_entries[spk]
                substantives = [(ts, txt) for ts, txt in entries if OfflineAsrService._is_substantive_turn(txt)]
                if not substantives:
                    substantives = entries
                ranked_idx = sorted(
                    range(len(substantives)),
                    key=lambda idx: len(substantives[idx][1]),
                    reverse=True,
                )
                chosen_indices: List[int] = []
                chosen_texts: List[str] = []
                for idx in ranked_idx:
                    cand_txt = substantives[idx][1]
                    if any(SequenceMatcher(None, cand_txt.lower(), prev.lower()).ratio() > 0.72 for prev in chosen_texts):
                        continue
                    chosen_indices.append(idx)
                    chosen_texts.append(cand_txt)
                    if len(chosen_indices) >= 6:
                        break
                chosen_indices.sort()
                pct = round((speaker_words[spk] / total_words) * 100)
                out.append(f"### {spk} ({speaker_turns[spk]} falas • {pct}%)")
                for idx in chosen_indices:
                    ts, txt = substantives[idx]
                    excerpt = txt if len(txt) <= 280 else txt[:277].rstrip() + "..."
                    out.append(f"- `[{ts}]` {excerpt}")
                out.append("")
        else:
            out.extend(["- Nenhum turno estruturado detectado.", ""])

        out.append("## 3. Linha do Tempo Consolidada por Bloco")
        if blocks:
            for blk in blocks:
                spks_blk = ", ".join(blk["speakers"]) if blk["speakers"] else "Sem identificação"
                out.append(f"### {blk['title']} *(Locutores: {spks_blk})*")
                subst_blk = [
                    (ts, spk, txt)
                    for ts, spk, txt in blk["entries"]
                    if OfflineAsrService._is_substantive_turn(txt)
                ]
                if not subst_blk:
                    subst_blk = blk["entries"]
                if len(subst_blk) > 6:
                    step = len(subst_blk) / 6.0
                    picked = [subst_blk[min(len(subst_blk) - 1, int(round(i * step)))] for i in range(6)]
                else:
                    picked = subst_blk
                seen_blk: List[str] = []
                for ts, spk, txt in picked:
                    if txt in seen_blk:
                        continue
                    seen_blk.append(txt)
                    excerpt = txt if len(txt) <= 240 else txt[:237].rstrip() + "..."
                    out.append(f"- `[{ts}]` **{spk}:** {excerpt}")
                out.append("")
        else:
            out.extend(["- Sem destaques cronológicos disponíveis.", ""])

        out.append("## 4. Decisões, Prazos e Pontos Críticos Identificados")
        if decisions or pains:
            if decisions:
                out.append("**Ações, prazos e encaminhamentos:**")
                out.extend(decisions)
                out.append("")
            if pains:
                out.append("**Gargalos e pontos de atenção relatados:**")
                out.extend(pains)
                out.append("")
        else:
            out.extend(["- Nenhum marcador explícito de decisão, prazo ou gargalo detectado na transcrição.", ""])

        out.extend(["## 5. Perguntas e Pontos em Aberto"])
        out.extend(questions if questions else ["- Nenhuma pergunta aberta identificada na transcrição."])
        out.append("")
        return "\n".join(out)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gerenciador de modelos offline do Escutaí")
    parser.add_argument("--prepare", choices=list(CT2_WHISPER_MODELS.keys()), default="turbo")
    args = parser.parse_args()

    svc = OfflineAsrService(default_variant=args.prepare)
    ok, msg = svc.ensure_models_ready(
        variant=args.prepare,
        progress_cb=lambda m, p: print(f"[{int(p * 100):3d}%] {m}", flush=True),
    )
    print(f"{'[OK]' if ok else '[ERRO]'} {msg}")
    sys.exit(0 if ok else 1)
