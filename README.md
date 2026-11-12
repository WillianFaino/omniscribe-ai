# 🎙️ OmniScribe AI

> **Universal Meeting Transcriber & Executive Minute Generator**  
> Captura dual sincronizada de áudio (Sistema/Reunião + Microfone), transcrição incremental em tempo real e consolidação de Atas Executivas estruturadas via **Google Gemini 2.5 Flash**.

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Google Gemini](https://img.shields.io/badge/Google%20Gemini-2.5%20Flash-4285F4?style=for-the-badge&logo=google&logoColor=white)](https://aistudio.google.com/)
[![CustomTkinter](https://img.shields.io/badge/UI-CustomTkinter-blue?style=for-the-badge)](https://github.com/TomSchimansky/CustomTkinter)
[![Windows](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D6?style=for-the-badge&logo=windows&logoColor=white)](https://microsoft.com)

---

## 🌟 Principais Recursos

- **Plataforma-Agnóstica:** Funciona perfeitamente com **Google Meet, Microsoft Teams, Zoom, Discord, Slack Huddles, WhatsApp Desktop** ou em **modo ditado exclusivo de microfone**.
- **Captura Dual WASAPI Loopback (`pyaudiowpatch`):** 
  - **Canal Esquerdo (L):** Captura o som de saída do Windows (participantes remotos, mesmo usando fones de ouvido).
  - **Canal Direito (R):** Captura seu microfone local.
- **Diferenciação Inteligente de Interlocutores:** O Gemini rotula automaticamente sua fala como `**Willian (Você)**` e os demais participantes como `**Participante A/B**` ou pelo nome real citado na conversa.
- **Atas Executivas Estruturadas em Markdown:** Ao encerrar, sintetiza:
  1. *Resumo Executivo*
  2. *Principais Tópicos com Horários*
  3. *Decisões Tomadas*
  4. *Tabela de Plano de Ação (Ação | Responsável | Prazo | Prioridade)*
  5. *Pendências e Pontos de Atenção*
- **Noise Gate & Modo Ausente:** Limiar de corte de ruído ajustável e botão de mudo rápido em tempo real.
- **Medidores VU em Tempo Real:** Barras visuais dinâmicas com resposta logarítmica para monitorar os níveis de volume de ambos os canais antes e durante a reunião.
- **Rastreador de Cotas e Custos da API:** Monitor em tempo real das 1.500 requisições diárias gratuitas do Free Tier do Gemini, total de áudio/tokens processados e estimativa em USD e R$ (BRL).
- **Histórico Visual de Gravações:** Navegue e visualize atas e transcrições passadas diretamente pela interface com um clique.
- **Assistente de Instalação Gráfico (Setup Wizard):** Instalador moderno executável para que qualquer usuário instale o aplicativo com dois cliques, escolha a pasta de destino e crie atalhos no Desktop e Menu Iniciar.
- **Inicialização Turbo (< 1s):** Empacotamento otimizado via PyInstaller em modo pré-extraído (`--onedir`), executável único (`--onefile`) ou instalador completo (`OmniScribe_Setup.exe`).

---

## 📁 Arquitetura do Projeto

```text
omniscribe-ai/
├── .env                          # Chave de API do Gemini (GEMINI_API_KEY=...)
├── .env.example                  # Modelo de configuração
├── .gitignore                    # Regras de exclusão de arquivos sensíveis e temporários
├── requirements.txt              # Dependências do projeto
├── build_exe.bat                 # Script automatizado de compilação e instalador
├── build_installer.bat           # Gerador dedicado do executável OmniScribe_Setup.exe
├── README.md                     # Documentação completa
├── src/
│   ├── main.py                   # Ponto de entrada, logging e boot da aplicação
│   ├── audio_engine.py           # Descoberta WASAPI, captura dual, resampling 16kHz e VU meters
│   ├── gemini_service.py         # Fila assíncrona, Gemini 2.5 Flash, limpeza de WAV e Ata
│   ├── usage_tracker.py          # Monitor de requisições, limites Free Tier e cálculo de custos
│   ├── installer.py              # Assistente visual de instalação (Setup Wizard CustomTkinter)
│   └── gui.py                    # Interface CustomTkinter (Dark Theme, abas, histórico e cotas)
└── atas_reunioes/                # Gerada automaticamente em runtime
    ├── usage_stats.json          # Persistência das métricas e limites da API
    └── AAAA/
        └── MM/
            ├── transcricao_YYYYMMDD_HHMM[_titulo].md
            └── ata_executiva_YYYYMMDD_HHMM[_titulo].md
```

---

## 🚀 Como Executar

### Pré-requisitos
- **Windows 10 ou 11** (64-bit)
- **Python 3.11+**
- Chave da API do Google Gemini (gratuita ou corporativa em: [Google AI Studio](https://aistudio.google.com/app/apikey))

### 1. Clonar e Instalar Dependências
```bash
# Clone o repositório
git clone https://github.com/SEU_USUARIO/omniscribe-ai.git
cd omniscribe-ai

# Crie um ambiente virtual (opcional, recomendado)
python -m venv venv
venv\Scripts\activate

# Instale os pacotes
pip install -r requirements.txt
```

### 2. Configurar a Chave da API
Crie um arquivo `.env` na raiz do projeto (ou copie de `.env.example`):
```env
GEMINI_API_KEY=sua_chave_gemini_aqui
```
*(Você também pode colar sua chave diretamente pela interface do aplicativo no botão **🔑 Configurar API Key**).*

### 3. Rodar em Modo Desenvolvimento
```bash
python src/main.py
```

---

## 📦 Gerando o Executável Windows (`.exe`)

Execute o script automatizado [build_exe.bat](build_exe.bat) dando **dois cliques** ou via terminal:
```cmd
build_exe.bat
```

Você pode escolher entre:
1. **[1] Modo Turbo Instantâneo (`--onedir` - Recomendado):** Abre em menos de 1 segundo! Gera a pasta `dist\OmniScribe\OmniScribe.exe`.
2. **[2] Executável Único (`--onefile`):** Gera um único arquivo portável em `dist\OmniScribe.exe`.
3. **[3] Assistente de Instalação Gráfico:** Gera o instalador autônomo profissional `dist\OmniScribe_Setup.exe`.

> 💡 **Dica:** Você também pode gerar o instalador diretamente executando `build_installer.bat`!

---

## 🔒 Privacidade e Governança
- **Processamento de Áudio Local:** O áudio é gravado e processado localmente em blocos temporários de WAV de 16kHz.
- **Upload Efêmero e Higienização:** Os blocos temporários enviados para a API de arquivos do Gemini são **automaticamente excluídos da nuvem** logo após a geração de cada bloco.
- **Limpeza Segura de Disco:** Os arquivos `.wav` locais são deletados do disco imediatamente após a confirmação da escrita da transcrição em Markdown.
- **Sem Dados Sensíveis no Git:** O arquivo `.env` e a pasta `atas_reunioes/` estão incluídos no `.gitignore`.

---

## 📄 Licença
Distribuído sob a licença MIT. Veja `LICENSE` para mais informações.

