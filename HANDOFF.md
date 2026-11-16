# Handoff Técnico — OmniScribe AI

> Documento de referência para continuidade de desenvolvimento, manutenção, build e distribuição do **OmniScribe AI**.
> Última atualização: 16/11/2026 (baseado no commit `5bdc492` na branch `main`, após PR #18).

---

## 1. Visão Geral do Projeto

O **OmniScribe AI** é uma aplicação desktop para **Windows 10/11** em Python (`3.11+`) que realiza:
1. **Captura dual sincronizada de áudio** via WASAPI (saída do sistema em loopback + microfone local).
2. **Transcrição incremental em blocos** usando a API do **Google Gemini (`gemini-2.5-flash`)** com diarização entre o usuário local (**Canal Direito / R**) e participantes remotos (**Canal Esquerdo / L**).
3. **Geração consolidada de Ata Executiva** estruturada em Markdown ao encerrar a sessão.
4. **Acompanhamento visual e interativo** por medidores VU, histórico de atas, monitor de cota/custo da API e o mascote 3D **OmniMic** estilo Windows 95/XP.

---

## 2. Estrutura do Repositório

```text
omniscribe-ai/
├── .env.example              # Template de configuração (GEMINI_API_KEY)
├── .gitignore                # Ignora build/, dist/, *.exe, *.zip, .env, atas_reunioes/, temp_blocks/, *.wav, *.blend1/2
├── AGENTS.md                 # Diretrizes de fluxo Git/PR e manutenção do HANDOFF.md
├── README.md                 # Documentação de uso e recursos
├── HANDOFF.md                # Este documento de handoff
├── requirements.txt          # Dependências Python
├── build_exe.bat             # Script interativo de build (--onedir, --onefile ou Setup)
├── build_installer.bat       # Script dedicado para gerar OmniScribe_Setup.exe
├── make_package.py           # Empacotador ZIP + compilador do instalador nativo Win32/.NET
├── assets/
│   ├── blender/
│   │   ├── omnimic_mascot.blend   # Cena inicial com rascunhos OmniMic_Detailed e OmniMic_V2_Exact
│   │   ├── omnimic_tpose.blend    # Modelo principal OmniMic_TPOSE em T-pose pronto para rigging
│   │   ├── omnimic_rigged.blend   # Cena rigada (OmniMic_Rig, 22 ossos) + 4 props PS1 + 7 animações em NLA
│   │   └── rig_omnimic.py         # Script reprodutível de rigging, props PS1, curvas Bezier e QA geométrico
│   ├── mascot/
│   │   ├── manifest.json          # Metadados das 8 tiras RGBA (`160x180`, 16-24 quadros) renderizadas do Blender
│   │   └── {idle,greeting,listening,transcribing,celebrating,warning,sleeping,spin}.png  # Sprites RGBA PS1
│   ├── models/
│   │   └── omnimic.glb            # Exportação GLB v2 (~906 KB) com skinning, 4 props e 7 Actions embutidas
│   └── references/
│       ├── omnimic_var1_more_serrilhado.jpg  # Referência visual aprovada (PS1 low-poly + Bayer dithering)
│       └── omnimic_tpose_{front,back,left,right,top,bottom}.jpg  # Turnaround ortográfico 6 vistas em T-pose
└── src/
    ├── __init__.py
    ├── main.py               # Entry point, sys.path, logging em atas_reunioes/logs/app.log e boot da GUI
    ├── audio_engine.py       # Descoberta WASAPI, captura dual, resampling 16 kHz, noise gate e VU meters
    ├── gemini_service.py     # Orquestrador híbrido (Gemini 2.5 + anti-loop + roteamento Offline) e Ata Executiva
    ├── offline_asr_service.py# Transcrição 100% offline (sherpa-onnx Whisper INT8 + VAD + Ollama/síntese local)
    ├── archive_service.py    # Pacote portátil SQLite (.omniscribe) com áudio comprimido (b4p_zlib) e segmentos
    ├── voiceprint_service.py # Biometria vocal ONNX (sherpa-onnx + ResNet34-LM + Silero VAD) e identificação automática
    ├── usage_tracker.py      # Persistência de uso diário (Free Tier 1.500 req/dia) e cálculo estimado USD/BRL
    ├── gui.py                # Interface CustomTkinter (ZoomTranscriberApp + modais VoiceProfiles, ApiKey, Usage, History e Retranscribe)
    ├── mascot_3d.py          # Renderizador 3D software Ultra Low-Poly (NumPy + Pillow) do OmniMic
    ├── mascot_widget.py      # Widget flutuante/arrastável OmniMicWidget com balão XP e máquina de estados
    ├── installer.py          # Setup Wizard em CustomTkinter (alternativa Python ao instalador nativo)
    └── installer_native.cs   # Stub WinForms/C# (~30 KB) que lê app_data.zip acoplado via EOF Overlay ('OMNISCRB')
```

---

## 3. Arquitetura e Fluxo de Execução

### 3.1 Inicialização (`src/main.py`)
- Garante `src/` no `sys.path`.
- Configura logging para `stdout` e `atas_reunioes/logs/app.log` via `setup_logging()` e `sys.excepthook`.
- Carrega `GEMINI_API_KEY` do `.env` (em `get_base_dir()`) ou variável de ambiente e inicia `run_gui()`.
- O SDK `google-genai` usa carregamento sob demanda (`_ensure_genai_loaded()` e `defer_init=True` no `GeminiService`) para manter o boot da interface rápido.

### 3.2 Motor de Áudio (`src/audio_engine.py`)
- **Descoberta de dispositivos (`listar_dispositivos_audio`)**: lista dispositivos WASAPI de loopback e microfones via `pyaudiowpatch` (com fallback para `pyaudio` ou lista mock caso indisponível).
- **Captura (`AudioEngine`)**:
  - Threads separadas para `_capture_loopback_loop` e `_capture_mic_loop`.
  - Modo mudo (`set_mic_muted`) e **noise gate suave** (`_apply_mic_noise_gate`, limiar padrão `0.015`, atenuação suave `0.15x` e *hold* de `150 ms` para preservar consoantes e falas baixas).
  - Thread `_vu_meter_loop` enviando RMS em dB normalizado (`0.0` a `1.0`) para a GUI.
  - Thread `_slicer_loop` fatiando blocos com **espera inteligente de pausa natural (até `4s`)** após `block_duration_seconds` (**`60s` padrão** para máxima precisão temporal, selecionável entre 1/3/5 min) e mantendo **buffer circular de overlap de `2,5s`** entre blocos consecutivos e no encerramento (`stop_recording(finalize_block=True)`).
- **Processamento DSP & WAV (`_create_block_file`)**:
  - Reamostragem anti-aliasing para **16 kHz** via `resample_to_16k` (`scipy.signal.resample_poly` com fallback FIR passa-baixa janela Hamming em NumPy).
  - Pré-processamento acústico (`enhance_speech_audio`): remoção de offset DC, filtro passa-alta linear-phase `~80 Hz`, normalização dinâmica por janelas (AGC suave RMS/pico com guarda de silêncio) e *limiter soft-knee* anti-clipping em `-1 dBFS`.
  - **Detecção automática de modo**:
    - **Mono Presencial (`1 canal`, `16000 Hz`)**: quando `_loopback_stream is None` e `_mic_stream is not None`.
    - **Estéreo Online (`2 canais`, `16000 Hz`)**: Canal Esquerdo (L) = Loopback / Sistema, Canal Direito (R) = Microfone local.
  - **Gravação Oficial Contínua (`session_wav_path`)**: ao informar `session_wav_path` em `start_recording`, os frames inéditos de cada bloco (descontado o prefixo de overlap de 2,5s) são gravados continuamente em `atas_reunioes/AAAA/MM/audio_YYYYMMDD_HHMM[_titulo].wav` para reprodução e reprocessamento futuro.
  - **Reprocessamento (`slice_wav_for_retranscription`)**: fatia um `.wav` salvo em blocos de `16 kHz` (com corte em pausa natural ±2s e overlap de 2,5s) mantendo o áudio original intacto.

### 3.3 Pipeline Gemini (`src/gemini_service.py`)
- `start_session(title, context_notes="", source_audio_path=None)` cria `atas_reunioes/AAAA/MM/transcricao_YYYYMMDD_HHMM[_titulo].md`, `ata_executiva_YYYYMMDD_HHMM[_titulo].md` e define `audio_YYYYMMDD_HHMM[_titulo].wav` (via `get_session_audio_path()`), extrai palavras-chave/glossário prioritário e inicia a thread `GeminiWorker`.
- Cada bloco WAV enfileirado (`enqueue_block`) passa por `_process_single_block`:
  1. Detecta automaticamente pelo cabeçalho `wave` se o WAV possui `1 canal` (selecionando `PRESENTIAL_TRANSCRIPTION_PROMPT` para diarização acústica em sala única com marcação `⚡ [Falas Sobrepostas]:`) ou `2 canais` (selecionando `TRANSCRIPTION_SYSTEM_PROMPT` estritamente separado L/R).
  2. Faz upload do `.wav` pela Files API (`self._client.files.upload`).
  3. Envia contexto seguro recente (`_build_safe_recent_context`, até 6 turnos válidos) + vocabulário prioritário + âncoras biométricas + prompt especializado (com regras anti-loop) para `gemini-2.5-flash` com `types.GenerateContentConfig(temperature=0.2, top_p=0.95, max_output_tokens=3072..8192)`.
  4. **Guarda Anti-Loop (`_detect_and_sanitize_repetition`)**: detecta repetições intra-linha (`5+x`), ciclos alternados de turnos (períodos 1–4) ou desvios de idioma/formato; em caso de colapso, executa 1 *retry* automático com `temperature=0.45` no mesmo arquivo remoto e saneia sobras antes de salvar.
  5. Registra áudio e tokens no `UsageTracker`, anexa no Markdown e apaga o `.wav` temporário somente após escrita confirmada (removendo o arquivo remoto no `finally`).
- No modo **Offline** (`engine_mode="offline"`), `_process_single_block_offline` transcreve localmente via `OfflineAsrService` (Whisper INT8 ONNX + Silero VAD + `VoiceprintService`) sem chamadas à nuvem.
- Ao receber a sentinela `None` via `finish_session()`, `_generate_executive_minutes()`:
  1. Em modo Online, executa **segunda passada fono-semântica** (`_refine_transcript_phonetics`, `temperature=0.15`, validada sobre o texto já limpo de loops) e gera a Ata via `MINUTES_SYSTEM_PROMPT`; em modo Offline, gera via **Ollama local** ou sintetizador determinístico estruturado.
  2. Quando `auto_export_sqlite` está ativo (padrão), empacota texto, metadados, segmentos e áudio compactado (`b4p_zlib`) em `atas_reunioes/AAAA/MM/reuniao_YYYYMMDD_HHMM[_titulo].omniscribe`.

### 3.4 Monitoramento de Cotas e Custos (`src/usage_tracker.py`)
- Persiste métricas em `atas_reunioes/usage_stats.json`.
- Zera `today_requests` automaticamente quando muda a data (`YYYY-MM-DD`).
- Referência de limite gratuito configurada: `FREE_TIER_DAILY_LIMIT_REQUESTS = 1500`.
- Estimativa de custo baseada nas constantes `COST_PER_AUDIO_SECOND_USD`, `COST_PER_INPUT_TOKEN_USD`, `COST_PER_OUTPUT_TOKEN_USD` e `DEFAULT_USD_TO_BRL = 5.80`.

### 3.4b Biometria Vocal / Voiceprints (`src/voiceprint_service.py`)
- Roda localmente em CPU via **`sherpa-onnx`** (`wespeaker_en_voxceleb_resnet34_LM.onnx`, 256 dims + `silero_vad.onnx`, ~26 MB sob demanda em `models/voiceprint/`, ignorados pelo `.gitignore`).
- Armazena perfis L2-normalizados em `atas_reunioes/voice_profiles.json` (`enroll_profile_from_arrays`, `enroll_profile_from_wav_segment`, `export_profiles` e `import_profiles` com validação de modelo `ResNet34-LM` e estratégias `merge`/`skip`/`overwrite`).
- Antes da chamada ao Gemini em `_process_single_block`, `analyze_wav_speakers()` segmenta o áudio Mono (ou Canal L remoto em Estéreo), gera um mapa temporal biométrico com similaridade de cosseno (incluindo detecção de possíveis sobreposições) e injeta as âncoras no prompt com **zero requisições extras no Free Tier**.

### 3.5 Interface Gráfica v2.0 (`src/gui.py`)
- Classe principal `ZoomTranscriberApp(ctk.CTk)` com paleta dupla `(Light, Dark)` de alto contraste (WCAG AA >= 4.5:1 no Dark/Light, **Modo Escuro** como padrão + alternador instantâneo `☀️ Claro / 🌙 Escuro` persistido em `atas_reunioes/ui_settings.json`) e navegação limpa em **4 abas principais** no topo (`🎙️ Gravação`, `📋 Ata`, `🗂️ Histórico`, `⚙️ Config & Voz`):
  - **Aba `🎙️ Gravação`**: título + glossário (50%/50%), sublinha limpa com `Captação` (`Online`/`Presencial`) e `Motor de IA` (`☁️ Gemini` / `💻 Offline`), botão único principal, cronômetro, medidores VU, barra de progresso assíncrona (`bar_async_progress`) e transcrição ao vivo.
  - **Aba `📋 Ata`**: leitura da Ata Executiva com `📂 Abrir Pasta`, `📦 Exportar .omniscribe` e `📋 Copiar Ata Markdown`.
  - **Aba `🗂️ Histórico`**: listagem nativa unificada de `.omniscribe`, `.wav` e `.md` via `render_history_cards_into()`, com **busca instantânea**, filtros por tipo, `📥 Importar .omniscribe`, player (`▶ Ouvir`), abertura direta e `🔄 Re-transcrever`.
  - **Aba `⚙️ Config & Voz`**: hardware WASAPI + bloco (1/3/5 min) + Noise Gate, `GEMINI_API_KEY` + medidor Free Tier, Voiceprints, card Offline (`Whisper small/base` + download com barra de progresso + detector Ollama + checkbox `.omniscribe`) e gaveta de logs técnicos recolhível.
- Modais auxiliares (`VoiceProfilesModal`, `RetranscribeModal` com seletor Online/Offline, `ApiKeyModal`, `UsageModal`, `HistoryModal`) estilizados uniformemente com `_style_option_menu`.

### 3.6 Mascote 3D OmniMic (`src/mascot_3d.py` e `src/mascot_widget.py`)
- **Renderização (`OmniMic3DRenderer`)**: carrega diretamente as **sprite sheets RGBA transparentes** de `assets/mascot/` (`idle`, `greeting`, `listening`, `transcribing`, `celebrating`, `warning`, `sleeping`, `spin`) renderizadas em Blender EEVEE com o shader `NG_PS1_Bayer4x4` e os 4 props oficiais, fazendo composição limpa sobre o fundo do tema ativo (com fallback automático para a malha procedural caso `assets/mascot/` não exista).
- **Interatividade (`OmniMicWidget`)**:
  - Overlay flutuante/arrastável no canto da janela com método `set_theme("Dark" | "Light")` sincronizando card, fundo do sprite e balão XP ao alternador de tema da GUI.
  - Reação a eventos da aplicação: `greeting`, `idle`, `sleeping` (após 45s sem interação), `listening` (com mini-mic LED ciano e velocidade sensível ao RMS), `transcribing` (bloquinho amarelo + lápis), `celebrating` (troféu dourado ao finalizar ata), `warning` e `spin` (rotação 360° em câmera orbital).
  - Visibilidade alternável pelo botão `🤖 OmniMic` na barra superior.
- **Assets 3D em Blender (`assets/blender/omnimic_tpose.blend` e `assets/references/`)**:
  - Turnaround ortográfico completo em T-pose nas 6 vistas (`omnimic_tpose_{front,back,left,right,top,bottom}.jpg`), derivado de `omnimic_var1_more_serrilhado.jpg`, com braços partindo da face externa do garfo U-yoke.
  - Cena principal `OmniMic_TPOSE` (`28` objetos, coleção `TPOSE_Lighting` com `6` luzes `AREA`, render `BLENDER_EEVEE` `768x768` com `Standard / None`).
  - Shader retrô PS1 em grupo `NG_PS1_Bayer4x4` (`ShaderToRGB -> Ambient Occlusion -> Layer Weight -> matriz Bayer 4x4` em `GridScale = 384.0`) alinhado ao compositor `Compositor_PS1` (`Pixelate Size = 2`), além de `Outline_Solidify` com backface culling e geometria refinada (expressão calma/preguiçosa nos olhos e sobrancelhas, luvas 3D com `3` costuras dorsais visíveis na vista traseira e facetas radiais na base inferior).
  - **Rigging, Props & Animações (`assets/blender/omnimic_rigged.blend` + `assets/models/omnimic.glb`):**
    - Braços subdivididos em `16` segmentos ao longo de X com *Dual Quaternion* (`use_deform_preserve_volume = True`) e pesos suaves entre `Torso_Yoke -> UpperArm -> Forearm -> Hand` + punho (`Cuff`) compartilhado para impedir qualquer deslocamento da mão.
    - Armature `OmniMic_Rig` (`22` ossos: `Root`, `Stand_Base`, `Torso_Yoke`, `Head`, olhos/pupilas/sobrancelhas `NegX`/`PosX`, `Mouth`, `Jaw_Lip`, cadeias conectadas `UpperArm -> Forearm -> Hand` L/R e `4` ossos de props: `Prop_Notepad`, `Prop_Pencil`, `Prop_MiniMic` e `Prop_Trophy`).
    - **4 Props low-poly no mesmo shader `NG_PS1_Bayer4x4` + `Outline_Solidify`:** bloquinho amarelo pautado (`Prop_Notepad`) + lápis hexagonal (`Prop_Pencil`) no `transcribing`, mini microfone direcional com anel LED ciano (`Prop_MiniMic`) no `listening`, e troféu estrela dourado (`Prop_Trophy`) no `celebrating`.
    - **7 animações detalhadas exportadas no `.glb`:** `idle` (96f), `greeting` (76f), `listening` (80f com mão em concha na lateral/orelha + mini-mic apontado para o monitor), `transcribing` (80f escrevendo 3 linhas no bloquinho com acompanhamento ocular), `celebrating` (72f com troféu + saltos), `warning` (64f) e `sleeping` (96f com micro-cochilo), validadas quadro a quadro contra colisão com a cabeça (`min_head_clear >= 6,2 cm`), deriva de punho (`<= 2,8 mm`) e arcos orgânicos de cotovelo (`17,2°–54,0°`, sem travas rígidas de `90°`).
  - **Diretriz consolidada (não repetir abordagens descartadas):** sprites 2D estáticos animados por escala/rotação, modelagem manual por `LatheGeometry` em Three.js e geradores web gratuitos com limite de exportação/polígonos foram descartados em favor do modelo próprio em **Blender** com *rigging* esquelético real.

#### Catálogo de Estados & Balão de Fala (Estilo Windows XP)
| Estado | Comportamento Esperado | Balão de Fala (`#FFFFE1`) |
|---|---|---|
| **Idle / Boas-vindas** | Acena com a mão articulada, pisca os olhos grandes, balança levemente | *"Olá! Pronto para anotar sua próxima reunião?"* |
| **Ouvindo ao Vivo** | Inclina-se atento; indicador LED ciano pulsa conforme o `RMS` do áudio | *"Captando áudio... pode deixar comigo!"* |
| **Transcrevendo / Gerando Ata** | Segura o bloquinho pautado amarelo e anota rápido com a caneta | *"Organizando os pontos principais na ata..."* |
| **Falas Sobrepostas / Ruído** | Arregala os olhos, arqueia as sobrancelhas e foca atento | *"Opa, muita gente falando junto! Redobrando a atenção."* |
| **Ata Concluída** | Ergue o troféu estrela dourado com brilhos comemorativos | *"Ata executiva pronta e salva!"* |
| **Alerta de Cota / Sem Chave** | Alerta visual apontando para o rastreador de API / configuração | *"Atenção: limite da API próximo ou chave não configurada."* |
| **Inativo / Soneca** | Fecha os olhos aos poucos após 45s e cochila; acorda ao clicar | Menu de clique direito: *"Ocultar assistente"* |

---

## 4. Como Executar e Empacotar

### Desenvolvimento local
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # preencher GEMINI_API_KEY
python src/main.py
```

### Build e Distribuição (Windows)
- **`build_exe.bat`**: oferece backup automático de `atas_reunioes/` e `.env` de builds anteriores e gera:
  - `[1]` `--onedir` (`dist/OmniScribe/OmniScribe.exe`) — inicialização rápida (`~3,25s` / `APP_STARTUP_MS=3252`) e build enxuto reduzido para **1.155 arquivos** via `--hidden-import "google.genai"` e exclusões no PyInstaller.
  - `[2]` `--onefile` (`dist/OmniScribe.exe`).
  - `[3]` Instalador nativo (`dist/OmniScribe_Setup.exe` + cópia na raiz).
- **`build_installer.bat`**: prepara/baixa os modelos ONNX locais (`python src/offline_asr_service.py --prepare small`, removendo pesos FP32 redundantes), copia `models\voiceprint` + `models\asr\sherpa-onnx-whisper-small` para `dist\OmniScribe\models\` e gera `dist/OmniScribe_Setup.exe` (**~417 MB** autocontido com modelos offline embutidos, abertura instantânea via stub C# `src/installer_native.cs` + payload EOF `OMNISCRB` em `make_package.py`).

---

## 5. Roadmap Futuro (Ideias Mapeadas)

### A. Melhorias na Captação de Voz & Diarização
1. **Identificação Consistente por Voz (Speaker Profiles):** *voice enrollment* rápido (5–10s) ou confirmação interativa no primeiro trecho para manter nomes fixos dos participantes ao longo de toda a reunião.
2. **Separação Estrita de Canais (Mic vs. Loopback):** processar/marcar explicitamente o canal do microfone (`Você`) separado do áudio do sistema (`Remoto`) antes do envio ao Gemini, melhorando a precisão em falas sobrepostas (*cross-talk*).
3. **VAD (Voice Activity Detection) + Overlap nos Cortes:** cortar blocos em pausas naturais de fala com pequena janela de sobreposição para não perder sílabas nas fronteiras.
4. **Pré-processamento de Áudio:** normalização automática de ganho (AGC) e filtro de ruído ambiente.

### B. Melhorias na Interface (UI/UX)
1. **Renomeação em Lote de Participantes:** clicar em `Participante A` na transcrição e substituir todas as ocorrências pelo nome real na transcrição e na ata.
2. **VU Meter Duplo ao Vivo:** barras independentes para **Microfone** e **Sistema**, com alerta suave caso o microfone esteja mutado durante uma reunião ativa.
3. **Busca, Filtros e Exportação Avançada:** pesquisa no histórico de gravações e exportação direta em `.md`, `.docx` e `.pdf`.
4. **Player Sincronizado com Timestamps:** clicar em uma fala da transcrição para reproduzir exatamente aquele trecho do áudio salvo.

### C. Transcrição Híbrida (Modo Online e Totalmente Offline) — **[IMPLEMENTADO]**
1. **Seletor de Modo na Interface (`Online` vs. `Offline`):** alternância direta na aba Gravação (`seg_engine_mode`) e no modal de Re-transcrição (`RetranscribeModal`), permitindo gravar e re-transcrever sem `GEMINI_API_KEY`.
2. **Pipeline Local Offline (`src/offline_asr_service.py`):** ASR local via `sherpa-onnx` **Whisper `small` / `base` INT8 ONNX** com download sob demanda, segmentação Silero VAD + diarização biométrica via `VoiceprintService` (`Estéreo L/R` e `Mono Presencial`) e geração da Ata via **Ollama local** (`localhost:11434`, quando ativo) ou sintetizador estruturado determinístico.

### D. Armazenamento Portátil em SQLite por Gravação — **[IMPLEMENTADO]**
1. **Arquivo Único Autocontido (`.omniscribe`):** empacotamento em SQLite (`src/archive_service.py`) contendo metadados, documentos Markdown (`transcrição` e `ata`), segmentos indexados (`transcript_segments`) e áudio oficial compactado em chunks via codec `b4p_zlib` (Block-4bit PCM + `zlib`, ~5x–9x menor sem depender de FFmpeg).
2. **Compartilhamento Direto & Modo Híbrido:** exportação automática ao finalizar sessão, botão **📦 Exportar .omniscribe** na aba Ata, e importação/leitura/playback/re-transcrição direta de `.omniscribe` na aba Histórico.

### E. Upload e Transcrição de Arquivos de Áudio e Vídeo
1. **Importação Direta (Drag-and-Drop / Seletor):** aceitar `.mp4`, `.mkv`, `.webm`, `.mov`, `.avi`, `.mp3`, `.wav`, `.m4a`, `.ogg` e `.flac` pela tela inicial.
2. **Extração & Segmentação Automática:** converter a faixa de áudio (via `ffmpeg` ou `av`/`pydub`) para chunks de `16 kHz` com barra de progresso e aplicar o mesmo pipeline de diarização e geração de Ata Executiva.

---

## 6. Estado Atual do Git e Pontos de Atenção

- **Branch base**: `main` (sincronizada com `origin/main` no commit `5bdc492`, após os PRs #14 anti-loop, #15 modos Offline 5.C e SQLite 5.D, #16 auditoria UI/UX Pro Max e #17/#18 empacotador com modelos ONNX embutidos).
- **Instalador Windows (`OmniScribe_Setup.exe`)**: compilado com `417.00 MB` (`1.205` arquivos, incluindo `models/voiceprint` e `models/asr/sherpa-onnx-whisper-small` INT8) para uso imediato offline desde a instalação.
- **Dependência externa principal (apenas modo Online)**: `GEMINI_API_KEY` válida (configurada via `.env` ou modal). No modo **Offline**, o app opera integralmente sem API Key.
- **Observações técnicas conhecidas no código atual**:
  - O rótulo do usuário local no prompt de diarização e cabeçalho de transcrição está definido como `Willian (Você)` em `src/gemini_service.py` e `src/offline_asr_service.py` (bom ponto para parametrizar futuramente caso o app seja distribuído para outros usuários).
  - O nome da classe principal em `src/gui.py` ainda é `ZoomTranscriberApp` (herança da fase inicial de MVP).
  - Os arquivos `src/installer.py` (CustomTkinter) e `src/installer_native.cs` (WinForms nativo) coexistem no repositório; o fluxo oficial dos `.bat` e `make_package.py` utiliza `src/installer_native.cs`.
  - Os testes de regressão/smoke são executados sob demanda via Python e ainda não estão organizados em uma suíte `pytest` versionada em `tests/`.
