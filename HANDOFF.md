# Handoff Técnico — OmniScribe AI

> Documento de referência para continuidade de desenvolvimento, manutenção, build e distribuição do **OmniScribe AI**.
> Última atualização: 16/11/2026 (baseado no commit `18b42c8` na branch `main`).

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
│   ├── models/
│   │   └── omnimic.glb            # Exportação GLB v2 (~906 KB) com skinning, 4 props e 7 Actions embutidas
│   └── references/
│       ├── omnimic_var1_more_serrilhado.jpg  # Referência visual aprovada (PS1 low-poly + Bayer dithering)
│       └── omnimic_tpose_{front,back,left,right,top,bottom}.jpg  # Turnaround ortográfico 6 vistas em T-pose
└── src/
    ├── __init__.py
    ├── main.py               # Entry point, sys.path, logging em atas_reunioes/logs/app.log e boot da GUI
    ├── audio_engine.py       # Descoberta WASAPI, captura dual, resampling 16 kHz, noise gate e VU meters
    ├── gemini_service.py     # Fila assíncrona de blocos WAV, upload/delete Files API, transcrição e Ata Executiva
    ├── usage_tracker.py      # Persistência de uso diário (Free Tier 1.500 req/dia) e cálculo estimado USD/BRL
    ├── gui.py                # Interface CustomTkinter (ZoomTranscriberApp + modais ApiKey, Usage e History)
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
  - Thread `_slicer_loop` fatiando blocos com **espera inteligente de pausa natural (até `4s`)** após `block_duration_seconds` (`180s` padrão) e mantendo **buffer circular de overlap de `2,5s`** entre blocos consecutivos e no encerramento (`stop_recording(finalize_block=True)`).
- **Processamento DSP & WAV (`_create_block_file`)**:
  - Reamostragem anti-aliasing para **16 kHz** via `resample_to_16k` (`scipy.signal.resample_poly` com fallback FIR passa-baixa janela Hamming em NumPy).
  - Pré-processamento acústico (`enhance_speech_audio`): remoção de offset DC, filtro passa-alta linear-phase `~80 Hz`, normalização dinâmica por janelas (AGC suave RMS/pico com guarda de silêncio) e *limiter soft-knee* anti-clipping em `-1 dBFS`.
  - **Detecção automática de modo**:
    - **Mono Presencial (`1 canal`, `16000 Hz`)**: quando `_loopback_stream is None` e `_mic_stream is not None`.
    - **Estéreo Online (`2 canais`, `16000 Hz`)**: Canal Esquerdo (L) = Loopback / Sistema, Canal Direito (R) = Microfone local.

### 3.3 Pipeline Gemini (`src/gemini_service.py`)
- `start_session(title, context_notes="")` cria `atas_reunioes/AAAA/MM/transcricao_YYYYMMDD_HHMM[_titulo].md` e `ata_executiva_YYYYMMDD_HHMM[_titulo].md`, extrai palavras-chave/glossário prioritário e inicia a thread `GeminiWorker`.
- Cada bloco WAV enfileirado (`enqueue_block`) passa por `_process_single_block`:
  1. Detecta automaticamente pelo cabeçalho `wave` se o WAV possui `1 canal` (selecionando `PRESENTIAL_TRANSCRIPTION_PROMPT` para diarização acústica em sala única com marcação `⚡ [Falas Sobrepostas]:`) ou `2 canais` (selecionando `TRANSCRIPTION_SYSTEM_PROMPT` estritamente separado L/R).
  2. Faz upload do `.wav` pela Files API (`self._client.files.upload`).
  3. Envia contexto recente (últimos 8 turnos) + vocabulário prioritário + prompt especializado para `gemini-2.5-flash` com `types.GenerateContentConfig(temperature=0.0, top_p=0.95)`.
  4. Registra áudio e tokens no `UsageTracker`.
  5. Anexa a transcrição ao arquivo Markdown e atualiza a GUI (`on_transcription_update`).
  6. **Segurança**: o `.wav` local só é removido após escrita confirmada com sucesso; em caso de falha, é preservado em disco.
  7. O arquivo remoto na nuvem é removido no bloco `finally` (`self._client.files.delete`).
- Ao receber a sentinela `None` via `finish_session()`, `_generate_executive_minutes()`:
  1. Executa **segunda passada fono-semântica** (`_refine_transcript_phonetics`, apenas texto, `temperature=0.0`, registrada no `UsageTracker`) para corrigir erros fonéticos pelo contexto global, unificar retroativamente nomes de participantes descobertos tardiamente e remover duplicações do overlap de `2,5s`.
  2. Atualiza o arquivo de transcrição revisado e sintetiza a Ata Executiva usando `MINUTES_SYSTEM_PROMPT` em 5 seções obrigatórias (Resumo Executivo, Principais Tópicos, Decisões Tomadas, Tabela de Plano de Ação e Pendências).

### 3.4 Monitoramento de Cotas e Custos (`src/usage_tracker.py`)
- Persiste métricas em `atas_reunioes/usage_stats.json`.
- Zera `today_requests` automaticamente quando muda a data (`YYYY-MM-DD`).
- Referência de limite gratuito configurada: `FREE_TIER_DAILY_LIMIT_REQUESTS = 1500`.
- Estimativa de custo baseada nas constantes `COST_PER_AUDIO_SECOND_USD`, `COST_PER_INPUT_TOKEN_USD`, `COST_PER_OUTPUT_TOKEN_USD` e `DEFAULT_USD_TO_BRL = 5.80`.

### 3.5 Interface Gráfica (`src/gui.py`)
- Classe principal `ZoomTranscriberApp(ctk.CTk)` com tema escuro e 3 abas principais (Transcrição ao Vivo, Ata Executiva e Logs).
- Modais auxiliares:
  - `ApiKeyModal`: configuração e persistência da `GEMINI_API_KEY` no `.env`.
  - `UsageModal`: visualização de requisições do dia, barra de cota Free Tier, tokens e custo estimado.
  - `HistoryModal`: listagem e abertura de atas/transcrições salvas em `atas_reunioes/`.

### 3.6 Mascote 3D OmniMic (`src/mascot_3d.py` e `src/mascot_widget.py`)
- **Renderização (`OmniMic3DRenderer`)**: malha articulada `Mesh3D` (`OmniMic #03-A Ultra Low-Poly`) renderizada em software (`128x144` ampliada com `NEAREST` para `192x216`), usando Painter's Algorithm, backface culling e iluminação flat-shading estilo Windows 95.
- **Interatividade (`OmniMicWidget`)**:
  - Overlay flutuante/arrastável no canto da janela com balão de fala estilo XP (`#FFFFE1`) e menu de contexto (`⋮` / clique direito).
  - Rastreamento do cursor do mouse nas pupilas.
  - Reação a eventos da aplicação: `greeting`, `idle`, `sleeping` (após 45s sem interação), `listening` (reage ao RMS do áudio), `transcribing` (segura bloquinho amarelo e caneta), `celebrating` (troféu dourado ao finalizar ata), `warning` e `spin`.
  - Visibilidade alternável pelo botão `🎙️ OmniMic` no rodapé da GUI.
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
- **`build_installer.bat`**: gera diretamente `dist/OmniScribe_Setup.exe` (**54,37 MB**, abertura verificada em **819 ms** sem extração prévia em `%TEMP%`) compilando `src/installer_native.cs` via `csc.exe` (.NET Framework 4.x) e acoplando `dist/app_data.zip` via `make_package.py` com assinatura `OMNISCRB`.

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

### C. Transcrição Híbrida (Modo Online e Totalmente Offline)
1. **Seletor de Modo na Interface (`Online` vs. `Offline`):** alternância rápida para reuniões confidenciais, ambientes sem internet ou cota esgotada.
2. **Pipeline Local Offline:** ASR local (`faster-whisper` ou `whisper.cpp` com modelos quantizados sob demanda), diarização local (`Mic` vs. `Loopback` + embeddings locais) e geração de ata via LLM local (`Ollama` / `llama.cpp`) ou fallback estruturado.

### D. Armazenamento Portátil em SQLite por Gravação
1. **Arquivo Único Autocontido (`.omniscribe` / `.db`):** empacotar áudio comprimido (**Opus/Ogg** ou **MP3** em `BLOB`), metadados, segmentos de transcrição e ata executiva em um único banco SQLite por reunião (reduzindo espaço em disco frente a `.wav` bruto).
2. **Compartilhamento Direto:** permitir abrir e compartilhar o arquivo `.omniscribe`/`.db` entre usuários do OmniScribe com áudio, transcrição e ata completos.

### E. Upload e Transcrição de Arquivos de Áudio e Vídeo
1. **Importação Direta (Drag-and-Drop / Seletor):** aceitar `.mp4`, `.mkv`, `.webm`, `.mov`, `.avi`, `.mp3`, `.wav`, `.m4a`, `.ogg` e `.flac` pela tela inicial.
2. **Extração & Segmentação Automática:** converter a faixa de áudio (via `ffmpeg` ou `av`/`pydub`) para chunks de `16 kHz` com barra de progresso e aplicar o mesmo pipeline de diarização e geração de Ata Executiva.

---

## 6. Estado Atual do Git e Pontos de Atenção

- **Branch base**: `main` (sincronizada com `origin/main` no commit `a26d751`, após o merge do PR #5 `docs/update-handoff-roadmap`).
- **Próxima etapa planejada (3D)**: integrar `assets/models/omnimic.glb` no widget flutuante `OmniMicWidget` (`WebView2` + `Three.js` preservando o visual PS1/Win95 ou pipeline de renderização dedicado), substituindo o renderizador procedural provisório em `src/mascot_3d.py`.
- **Dependência externa principal**: `GEMINI_API_KEY` válida (configurada via `.env` ou modal na primeira execução).
- **Observações técnicas conhecidas no código atual**:
  - O rótulo do usuário local no prompt de diarização e cabeçalho de transcrição está definido como `Willian (Você)` em `src/gemini_service.py` (bom ponto para parametrizar futuramente caso o app seja distribuído para outros usuários).
  - O nome da classe principal em `src/gui.py` ainda é `ZoomTranscriberApp` (herança da fase inicial de MVP).
  - Os arquivos `src/installer.py` (CustomTkinter) e `src/installer_native.cs` (WinForms nativo) coexistem no repositório; o fluxo oficial dos `.bat` e `make_package.py` utiliza `src/installer_native.cs`.
  - Ainda não há suíte de testes automatizados versionada no repositório.
