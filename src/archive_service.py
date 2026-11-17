"""Serviço de Armazenamento Portátil em SQLite (`.escutai` / legado `.omniscribe`) do Escutaí.

Empacota uma reunião completa em um único arquivo SQLite autocontido (`.escutai`):
- Metadados estruturados (título, data, duração, modo de captura, engine, glossário);
- Transcrição integral + segmentos indexados (timestamp, locutor, fala, sobreposição);
- Ata Executiva em Markdown;
- Faixa de áudio oficial compactada em chunks via codec Block-4bit + zlib (`b4p_zlib`)
  em NumPy puro (~4,5x a 5x menor que WAV bruto, sem dependência de FFmpeg/PyAV).
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import struct
import wave
import zlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("omniscribe.archive")

PRIMARY_ARCHIVE_EXT = ".escutai"
SUPPORTED_ARCHIVE_EXTS = (".escutai", ".omniscribe")
ARCHIVE_SCHEMA_VERSION = "1"
AUDIO_CODEC_B4P_ZLIB = "b4p_zlib"
BLOCK_SAMPLES = 32
CHUNK_SECONDS = 10.0

_TURN_LINE_RE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s+\*\*(.+?)\*\*:\s*(.*)$")
_OVERLAP_HDR_RE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s+⚡")
_OVERLAP_ITEM_RE = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*:\s*(.*)$")
_BLOCK_HDR_RE = re.compile(r"^###\s+Bloco\s+(\d+)\s+\[(\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*(\d{1,2}:\d{2}(?::\d{2})?)\]")


def _ts_to_seconds(ts: str) -> float:
    parts = [int(p) for p in ts.strip().split(":")]
    if len(parts) == 3:
        return float(parts[0] * 3600 + parts[1] * 60 + parts[2])
    if len(parts) == 2:
        return float(parts[0] * 60 + parts[1])
    return 0.0


def encode_audio_b4p_zlib(pcm_int16: np.ndarray, channels: int = 1) -> Tuple[bytes, int]:
    """Comprime array PCM int16 `(frames * channels,)` ou `(frames, channels)` em ~4,5x usando blocos de 32 amostras + zlib."""
    arr = np.asarray(pcm_int16, dtype=np.int16)
    if arr.ndim == 1:
        if channels > 1:
            usable = (len(arr) // channels) * channels
            arr = arr[:usable].reshape(-1, channels)
        else:
            arr = arr.reshape(-1, 1)

    num_frames = int(arr.shape[0])
    ch = int(arr.shape[1]) if arr.ndim > 1 else 1
    if num_frames == 0:
        return b"", 0

    flat = arr.astype(np.float32).reshape(-1)
    pad = (-len(flat)) % BLOCK_SAMPLES
    if pad:
        flat = np.concatenate([flat, np.zeros(pad, dtype=np.float32)])

    blocks = flat.reshape(-1, BLOCK_SAMPLES)
    scales = np.maximum(np.max(np.abs(blocks), axis=1), 1.0) / 7.0
    q4 = np.clip(np.round(blocks / scales[:, None]), -7, 7).astype(np.int8) + 8
    packed = ((q4[:, 0::2].astype(np.uint8) & 0x0F) << 4) | (q4[:, 1::2].astype(np.uint8) & 0x0F)

    n_blocks = int(blocks.shape[0])
    raw_payload = (
        struct.pack("<IH", num_frames, ch)
        + struct.pack("<I", n_blocks)
        + scales.astype(np.float16).tobytes()
        + packed.tobytes()
    )
    return zlib.compress(raw_payload, level=6), num_frames


def decode_audio_b4p_zlib(blob: bytes) -> Tuple[np.ndarray, int]:
    """Descomprime blob `b4p_zlib` de volta para array PCM int16 `(frames * channels,)` e número de canais."""
    if not blob:
        return np.empty(0, dtype=np.int16), 1

    raw = zlib.decompress(blob)
    if len(raw) < 10:
        return np.empty(0, dtype=np.int16), 1

    num_frames, ch, n_blocks = struct.unpack("<IHI", raw[:10])
    scales_bytes = n_blocks * 2
    scales_end = 10 + scales_bytes
    scales = np.frombuffer(raw[10:scales_end], dtype=np.float16).astype(np.float32)
    packed = np.frombuffer(raw[scales_end:], dtype=np.uint8).reshape(n_blocks, BLOCK_SAMPLES // 2)

    q_vals = np.empty((n_blocks, BLOCK_SAMPLES), dtype=np.float32)
    q_vals[:, 0::2] = (packed >> 4).astype(np.int8) - 8
    q_vals[:, 1::2] = (packed & 0x0F).astype(np.int8) - 8
    decoded = np.clip(np.round((q_vals * scales[:, None]).reshape(-1)), -32768, 32767).astype(np.int16)

    needed = num_frames * max(1, ch)
    return decoded[:needed], max(1, ch)


def parse_transcript_segments(transcript_md: str) -> List[Dict]:
    """Extrai segmentos estruturados (bloco, timestamp, locutor, fala) do Markdown de transcrição."""
    segments: List[Dict] = []
    current_block = 1
    current_block_end = 0.0
    in_overlap_ts: Optional[str] = None

    for raw_line in (transcript_md or "").splitlines():
        line = raw_line.strip()
        if not line:
            in_overlap_ts = None
            continue

        blk_m = _BLOCK_HDR_RE.match(line)
        if blk_m:
            current_block = int(blk_m.group(1))
            current_block_end = _ts_to_seconds(blk_m.group(3))
            in_overlap_ts = None
            continue

        ov_hdr = _OVERLAP_HDR_RE.match(line)
        if ov_hdr:
            in_overlap_ts = ov_hdr.group(1)
            continue

        if in_overlap_ts is not None:
            ov_item = _OVERLAP_ITEM_RE.match(line)
            if ov_item:
                sec = _ts_to_seconds(in_overlap_ts)
                segments.append(
                    {
                        "block_index": current_block,
                        "start_seconds": sec,
                        "end_seconds": max(sec, current_block_end),
                        "timestamp_str": in_overlap_ts,
                        "speaker": ov_item.group(1).strip(),
                        "text": ov_item.group(2).strip(),
                        "is_overlap": 1,
                    }
                )
                continue
            in_overlap_ts = None

        turn_m = _TURN_LINE_RE.match(line)
        if turn_m:
            ts = turn_m.group(1)
            sec = _ts_to_seconds(ts)
            segments.append(
                {
                    "block_index": current_block,
                    "start_seconds": sec,
                    "end_seconds": max(sec, current_block_end),
                    "timestamp_str": ts,
                    "speaker": turn_m.group(2).strip(),
                    "text": turn_m.group(3).strip(),
                    "is_overlap": 0,
                }
            )

    return segments


class MeetingArchiveService:
    """Cria, lê, importa e extrai pacotes portáteis `.escutai` (e `.omniscribe` legado) em SQLite."""

    @staticmethod
    def _open_conn(db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=DELETE;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS documents (
                doc_type TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transcript_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                block_index INTEGER NOT NULL,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                timestamp_str TEXT NOT NULL,
                speaker TEXT NOT NULL,
                text TEXT NOT NULL,
                is_overlap INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS audio_chunks (
                chunk_index INTEGER PRIMARY KEY,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                num_frames INTEGER NOT NULL,
                codec TEXT NOT NULL,
                payload BLOB NOT NULL
            );
            """
        )

    def export_meeting_package(
        self,
        output_path: Path,
        title: str,
        created_at: Optional[datetime] = None,
        mode: str = "",
        engine_mode: str = "online",
        context_notes: str = "",
        transcript_md: str = "",
        minutes_md: str = "",
        wav_path: Optional[Path] = None,
    ) -> Dict:
        """Empacota metadados, transcrição, ata e áudio compactado em um pacote `.escutai` único."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

        now_iso = (created_at or datetime.now()).isoformat(timespec="seconds")
        channels = 1
        sample_rate = 16000
        duration_seconds = 0.0
        raw_pcm_bytes = 0
        compressed_audio_bytes = 0
        chunk_rows: List[Tuple[int, float, float, int, str, bytes]] = []

        if wav_path is not None and Path(wav_path).exists():
            wav_p = Path(wav_path)
            with wave.open(str(wav_p), "rb") as wf:
                channels = max(1, int(wf.getnchannels()))
                sampwidth = int(wf.getsampwidth())
                sample_rate = max(1, int(wf.getframerate()))
                total_frames = int(wf.getnframes())
                duration_seconds = total_frames / float(sample_rate)
                raw_pcm_bytes = total_frames * channels * sampwidth

                if sampwidth == 2 and total_frames > 0:
                    chunk_frames = max(BLOCK_SAMPLES, int(round(CHUNK_SECONDS * sample_rate)))
                    chunk_idx = 0
                    frames_read = 0
                    while frames_read < total_frames:
                        to_read = min(chunk_frames, total_frames - frames_read)
                        raw_bytes = wf.readframes(to_read)
                        pcm = np.frombuffer(raw_bytes, dtype=np.int16)
                        blob, actual_frames = encode_audio_b4p_zlib(pcm, channels=channels)
                        s_t = frames_read / float(sample_rate)
                        e_t = (frames_read + actual_frames) / float(sample_rate)
                        chunk_rows.append((chunk_idx, s_t, e_t, actual_frames, AUDIO_CODEC_B4P_ZLIB, blob))
                        compressed_audio_bytes += len(blob)
                        frames_read += to_read
                        chunk_idx += 1

        segments = parse_transcript_segments(transcript_md)

        conn = self._open_conn(tmp_path)
        try:
            self._init_schema(conn)
            meta_items = {
                "schema_version": ARCHIVE_SCHEMA_VERSION,
                "title": title.strip() or "Reunião",
                "created_at": now_iso,
                "duration_seconds": f"{duration_seconds:.2f}",
                "mode": mode,
                "engine_mode": engine_mode,
                "context_notes": context_notes,
                "channels": str(channels),
                "sample_rate": str(sample_rate),
                "codec": AUDIO_CODEC_B4P_ZLIB if chunk_rows else "none",
                "raw_pcm_bytes": str(raw_pcm_bytes),
                "compressed_audio_bytes": str(compressed_audio_bytes),
                "segment_count": str(len(segments)),
                "source_wav_name": Path(wav_path).name if wav_path else "",
            }
            conn.executemany(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                list(meta_items.items()),
            )
            conn.execute(
                "INSERT OR REPLACE INTO documents (doc_type, content, updated_at) VALUES (?, ?, ?)",
                ("transcript_md", transcript_md or "", now_iso),
            )
            conn.execute(
                "INSERT OR REPLACE INTO documents (doc_type, content, updated_at) VALUES (?, ?, ?)",
                ("minutes_md", minutes_md or "", now_iso),
            )
            if segments:
                conn.executemany(
                    """
                    INSERT INTO transcript_segments
                        (block_index, start_seconds, end_seconds, timestamp_str, speaker, text, is_overlap)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            seg["block_index"],
                            seg["start_seconds"],
                            seg["end_seconds"],
                            seg["timestamp_str"],
                            seg["speaker"],
                            seg["text"],
                            seg["is_overlap"],
                        )
                        for seg in segments
                    ],
                )
            if chunk_rows:
                conn.executemany(
                    """
                    INSERT INTO audio_chunks
                        (chunk_index, start_seconds, end_seconds, num_frames, codec, payload)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    chunk_rows,
                )
            conn.commit()
        finally:
            conn.close()

        tmp_path.replace(output_path)
        archive_size = output_path.stat().st_size
        ratio = (raw_pcm_bytes / float(compressed_audio_bytes)) if compressed_audio_bytes > 0 else 1.0
        return {
            "archive_path": str(output_path),
            "size_bytes": archive_size,
            "raw_pcm_bytes": raw_pcm_bytes,
            "compressed_audio_bytes": compressed_audio_bytes,
            "compression_ratio": round(ratio, 2),
            "segment_count": len(segments),
            "chunk_count": len(chunk_rows),
            "duration_seconds": duration_seconds,
        }

    def read_archive_summary(self, archive_path: Path) -> Dict:
        """Lê metadados resumidos de um pacote `.omniscribe` sem decodificar o áudio."""
        archive_path = Path(archive_path)
        conn = sqlite3.connect(f"file:{archive_path}?mode=ro", uri=True)
        try:
            meta = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
            docs = {
                row[0]: len(row[1] or "")
                for row in conn.execute("SELECT doc_type, content FROM documents").fetchall()
            }
            chunk_count = int(conn.execute("SELECT COUNT(*) FROM audio_chunks").fetchone()[0])
            size_bytes = archive_path.stat().st_size
            raw_pcm = int(meta.get("raw_pcm_bytes", "0") or 0)
            comp_audio = int(meta.get("compressed_audio_bytes", "0") or 0)
            return {
                "path": archive_path,
                "title": meta.get("title") or archive_path.stem,
                "created_at": meta.get("created_at", ""),
                "duration_seconds": float(meta.get("duration_seconds", "0") or 0.0),
                "channels": int(meta.get("channels", "1") or 1),
                "sample_rate": int(meta.get("sample_rate", "16000") or 16000),
                "engine_mode": meta.get("engine_mode", "online"),
                "codec": meta.get("codec", "none"),
                "segment_count": int(meta.get("segment_count", "0") or 0),
                "chunk_count": chunk_count,
                "has_audio": chunk_count > 0,
                "has_transcript": docs.get("transcript_md", 0) > 0,
                "has_minutes": docs.get("minutes_md", 0) > 0,
                "raw_pcm_bytes": raw_pcm,
                "compressed_audio_bytes": comp_audio,
                "compression_ratio": round(raw_pcm / float(comp_audio), 2) if comp_audio > 0 else 1.0,
                "size_bytes": size_bytes,
                "context_notes": meta.get("context_notes", ""),
            }
        finally:
            conn.close()

    def load_archive_contents(self, archive_path: Path) -> Dict:
        """Carrega documentos (`transcript_md`, `minutes_md`) e metadados do `.omniscribe`."""
        archive_path = Path(archive_path)
        summary = self.read_archive_summary(archive_path)
        conn = sqlite3.connect(f"file:{archive_path}?mode=ro", uri=True)
        try:
            docs = dict(conn.execute("SELECT doc_type, content FROM documents").fetchall())
            rows = conn.execute(
                """
                SELECT block_index, start_seconds, end_seconds, timestamp_str, speaker, text, is_overlap
                FROM transcript_segments ORDER BY id ASC
                """
            ).fetchall()
            segments = [
                {
                    "block_index": r[0],
                    "start_seconds": r[1],
                    "end_seconds": r[2],
                    "timestamp_str": r[3],
                    "speaker": r[4],
                    "text": r[5],
                    "is_overlap": bool(r[6]),
                }
                for r in rows
            ]
            summary["transcript_md"] = docs.get("transcript_md", "")
            summary["minutes_md"] = docs.get("minutes_md", "")
            summary["segments"] = segments
            return summary
        finally:
            conn.close()

    def extract_wav(self, archive_path: Path, dest_wav_path: Optional[Path] = None) -> Optional[Path]:
        """Reconstrói um WAV 16-bit PCM reproduzível a partir dos chunks comprimidos no `.omniscribe`."""
        archive_path = Path(archive_path)
        conn = sqlite3.connect(f"file:{archive_path}?mode=ro", uri=True)
        try:
            meta = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
            channels = max(1, int(meta.get("channels", "1") or 1))
            sample_rate = max(8000, int(meta.get("sample_rate", "16000") or 16000))
            rows = conn.execute(
                "SELECT codec, payload FROM audio_chunks ORDER BY chunk_index ASC"
            ).fetchall()
            if not rows:
                return None

            out_path = Path(dest_wav_path) if dest_wav_path else archive_path.with_suffix(".wav")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(out_path), "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                for codec, payload in rows:
                    if codec == AUDIO_CODEC_B4P_ZLIB:
                        pcm, _ch = decode_audio_b4p_zlib(payload)
                        if len(pcm) > 0:
                            wf.writeframes(pcm.tobytes())
                    else:
                        wf.writeframes(payload)
            return out_path
        finally:
            conn.close()

    def import_archive_to_workspace(
        self,
        source_archive: Path,
        target_dir: Path,
        extract_documents: bool = True,
    ) -> Dict:
        """Copia um `.omniscribe` compartilhado para o workspace sem sobrescrever existentes."""
        source_archive = Path(source_archive).resolve()
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        summary = self.read_archive_summary(source_archive)
        dest_archive = target_dir / source_archive.name
        if dest_archive.resolve() != source_archive:
            idx = 1
            while dest_archive.exists():
                dest_archive = target_dir / f"{source_archive.stem}_import_{idx}{source_archive.suffix}"
                idx += 1
            shutil.copy2(source_archive, dest_archive)

        contents = self.load_archive_contents(dest_archive)
        extracted_paths: Dict[str, Optional[Path]] = {"archive": dest_archive, "transcript": None, "minutes": None}

        if extract_documents:
            stem = dest_archive.stem
            if contents.get("transcript_md", "").strip():
                t_path = target_dir / f"transcricao_{stem}.md"
                if not t_path.exists():
                    t_path.write_text(contents["transcript_md"], encoding="utf-8")
                extracted_paths["transcript"] = t_path
            if contents.get("minutes_md", "").strip():
                m_path = target_dir / f"ata_executiva_{stem}.md"
                if not m_path.exists():
                    m_path.write_text(contents["minutes_md"], encoding="utf-8")
                extracted_paths["minutes"] = m_path

        contents["imported_paths"] = extracted_paths
        return contents
