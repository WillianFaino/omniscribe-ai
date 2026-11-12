"""Módulo de rastreamento de consumo, cotas e custos estimados da API Gemini.

Registra requisições diárias para monitoramento do limite Free Tier (1.500 req/dia)
e calcula custos estimados em dólares (USD) e reais (BRL).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("omniscribe.usage_tracker")

# Preços de referência do Gemini 2.5 Flash (Google AI Studio / Vertex AI)
# Entrada de Áudio: ~$0.07 / hora (~$0.00001944 por segundo)
COST_PER_AUDIO_SECOND_USD = 0.07 / 3600.0
# Entrada de Texto: $0.075 por 1 milhão de tokens
COST_PER_INPUT_TOKEN_USD = 0.075 / 1_000_000.0
# Saída de Texto: $0.30 por 1 milhão de tokens
COST_PER_OUTPUT_TOKEN_USD = 0.30 / 1_000_000.0

# Limite diário gratuito do plano Free Tier do Google AI Studio
FREE_TIER_DAILY_LIMIT_REQUESTS = 1500
DEFAULT_USD_TO_BRL = 5.80


@dataclass
class UsageData:
    total_audio_seconds: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_requests: int = 0
    total_meetings: int = 0
    today_date: str = ""
    today_requests: int = 0
    estimated_cost_usd: float = 0.0


class UsageTracker:
    """Gerenciador persistente de estatísticas de uso e cotas da API."""

    def __init__(self, storage_dir: Path):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.stats_file = self.storage_dir / "usage_stats.json"
        self.data = self._load()

    def _load(self) -> UsageData:
        today_str = datetime.now().strftime("%Y-%m-%d")
        if self.stats_file.exists():
            try:
                with open(self.stats_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                data = UsageData(**raw)
                # Reseta contador diário se mudou de dia
                if data.today_date != today_str:
                    data.today_date = today_str
                    data.today_requests = 0
                return data
            except Exception as e:
                logger.warning(f"Erro ao carregar usage_stats.json: {e}")

        return UsageData(today_date=today_str, today_requests=0)

    def _save(self) -> None:
        try:
            with open(self.stats_file, "w", encoding="utf-8") as f:
                json.dump(asdict(self.data), f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Erro ao salvar usage_stats.json: {e}")

    def register_request(
        self,
        audio_seconds: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        is_new_meeting: bool = False,
    ) -> None:
        """Registra uma requisição bem-sucedida à API do Gemini."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        if self.data.today_date != today_str:
            self.data.today_date = today_str
            self.data.today_requests = 0

        self.data.total_requests += 1
        self.data.today_requests += 1
        self.data.total_audio_seconds += max(0.0, audio_seconds)
        self.data.total_input_tokens += max(0, input_tokens)
        self.data.total_output_tokens += max(0, output_tokens)

        if is_new_meeting:
            self.data.total_meetings += 1

        # Atualiza custo estimado
        cost_audio = self.data.total_audio_seconds * COST_PER_AUDIO_SECOND_USD
        cost_input = self.data.total_input_tokens * COST_PER_INPUT_TOKEN_USD
        cost_output = self.data.total_output_tokens * COST_PER_OUTPUT_TOKEN_USD
        self.data.estimated_cost_usd = cost_audio + cost_input + cost_output

        self._save()

    def get_summary(self) -> Dict:
        """Retorna resumo estruturado para exibição na interface."""
        cost_usd = self.data.estimated_cost_usd
        cost_brl = cost_usd * DEFAULT_USD_TO_BRL
        daily_percent = min(100.0, (self.data.today_requests / float(FREE_TIER_DAILY_LIMIT_REQUESTS)) * 100.0)

        total_mins = int(self.data.total_audio_seconds // 60)
        total_hours = total_mins // 60
        rem_mins = total_mins % 60
        time_str = f"{total_hours}h {rem_mins}m" if total_hours > 0 else f"{total_mins} min"

        return {
            "total_meetings": self.data.total_meetings,
            "total_requests": self.data.total_requests,
            "today_requests": self.data.today_requests,
            "free_limit": FREE_TIER_DAILY_LIMIT_REQUESTS,
            "daily_percent": daily_percent,
            "audio_time_str": time_str,
            "total_tokens": self.data.total_input_tokens + self.data.total_output_tokens,
            "cost_usd": cost_usd,
            "cost_brl": cost_brl,
        }
