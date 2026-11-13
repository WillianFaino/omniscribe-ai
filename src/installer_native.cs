using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace OmniScribeSetup
{
    internal static class NativeMethods
    {
        [DllImport("user32.dll")]
        internal static extern bool SetProcessDPIAware();

        [DllImport("dwmapi.dll")]
        internal static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int attrValue, int attrSize);

        internal static void EnableDarkTitleBar(IntPtr handle)
        {
            try
            {
                int useDark = 1;
                DwmSetWindowAttribute(handle, 20, ref useDark, sizeof(int));
            }
            catch
            {
            }
        }
    }

    internal sealed class SubStream : Stream
    {
        private readonly Stream _baseStream;
        private readonly long _start;
        private readonly long _length;
        private long _position;

        public SubStream(Stream baseStream, long start, long length)
        {
            _baseStream = baseStream;
            _start = start;
            _length = length;
            _position = 0;
            _baseStream.Seek(_start, SeekOrigin.Begin);
        }

        public override bool CanRead { get { return true; } }
        public override bool CanSeek { get { return true; } }
        public override bool CanWrite { get { return false; } }
        public override long Length { get { return _length; } }

        public override long Position
        {
            get { return _position; }
            set { Seek(value, SeekOrigin.Begin); }
        }

        public override void Flush() { }

        public override int Read(byte[] buffer, int offset, int count)
        {
            long remaining = _length - _position;
            if (remaining <= 0) return 0;
            if (count > remaining) count = (int)remaining;
            _baseStream.Seek(_start + _position, SeekOrigin.Begin);
            int read = _baseStream.Read(buffer, offset, count);
            _position += read;
            return read;
        }

        public override long Seek(long offset, SeekOrigin origin)
        {
            long target = _position;
            if (origin == SeekOrigin.Begin) target = offset;
            else if (origin == SeekOrigin.Current) target = _position + offset;
            else if (origin == SeekOrigin.End) target = _length + offset;

            if (target < 0) target = 0;
            if (target > _length) target = _length;
            _position = target;
            _baseStream.Seek(_start + _position, SeekOrigin.Begin);
            return _position;
        }

        public override void SetLength(long value) { throw new NotSupportedException(); }
        public override void Write(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }
    }

    internal sealed class FlatButton : Button
    {
        public FlatButton()
        {
            FlatStyle = FlatStyle.Flat;
            FlatAppearance.BorderSize = 0;
            Cursor = Cursors.Hand;
            Font = new Font("Segoe UI", 9.5f, FontStyle.Bold);
        }
    }

    internal sealed class ModernProgressBar : Panel
    {
        private double _value = 0.0;
        private Color _fillColor = Color.FromArgb(59, 130, 246);

        public ModernProgressBar()
        {
            BackColor = Color.FromArgb(31, 41, 55);
            Height = 16;
            DoubleBuffered = true;
        }

        public double Value
        {
            get { return _value; }
            set
            {
                if (value < 0.0) value = 0.0;
                if (value > 1.0) value = 1.0;
                _value = value;
                Invalidate();
            }
        }

        public Color FillColor
        {
            get { return _fillColor; }
            set { _fillColor = value; Invalidate(); }
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            int w = (int)(Width * _value);
            if (w > 0)
            {
                using (SolidBrush brush = new SolidBrush(_fillColor))
                {
                    e.Graphics.FillRectangle(brush, 0, 0, w, Height);
                }
            }
        }
    }

    public sealed class InstallerForm : Form
    {
        private static readonly Color BgColor = Color.FromArgb(11, 15, 25);
        private static readonly Color CardColor = Color.FromArgb(17, 24, 39);
        private static readonly Color BorderColor = Color.FromArgb(31, 41, 55);
        private static readonly Color InputBg = Color.FromArgb(7, 11, 18);
        private static readonly Color TextMain = Color.FromArgb(248, 250, 252);
        private static readonly Color TextMuted = Color.FromArgb(148, 163, 184);
        private static readonly Color AccentBlue = Color.FromArgb(59, 130, 246);
        private static readonly Color AccentGreen = Color.FromArgb(16, 185, 129);
        private static readonly Color BtnGray = Color.FromArgb(55, 65, 81);

        private int _step = 1;
        private string _targetDir;
        private bool _desktopShortcut = true;
        private bool _startShortcut = true;
        private bool _preserveData = true;
        private bool _launchNow = true;

        private Panel _pageContainer;
        private Panel _navBar;
        private FlatButton _btnBack;
        private FlatButton _btnCancel;
        private FlatButton _btnNext;

        private TextBox _txtDir;
        private CheckBox _chkDesktop;
        private CheckBox _chkStart;
        private CheckBox _chkPreserve;
        private CheckBox _chkLaunch;
        private ModernProgressBar _progressBar;
        private Label _lblStatus;

        public InstallerForm()
        {
            Text = "Instalador do OmniScribe AI";
            ClientSize = new Size(640, 480);
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = BgColor;
            ForeColor = TextMain;
            Font = new Font("Segoe UI", 9.5f);

            string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            _targetDir = Path.Combine(localAppData, "Programs", "OmniScribe AI");

            BuildShell();
            ShowStep(1);
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            NativeMethods.EnableDarkTitleBar(Handle);
        }

        private void BuildShell()
        {
            _navBar = new Panel();
            _navBar.Dock = DockStyle.Bottom;
            _navBar.Height = 64;
            _navBar.BackColor = CardColor;

            Panel borderLine = new Panel();
            borderLine.Dock = DockStyle.Top;
            borderLine.Height = 1;
            borderLine.BackColor = BorderColor;
            _navBar.Controls.Add(borderLine);

            _btnBack = new FlatButton();
            _btnBack.Text = "< Voltar";
            _btnBack.Size = new Size(105, 36);
            _btnBack.Location = new Point(22, 14);
            _btnBack.BackColor = BtnGray;
            _btnBack.ForeColor = TextMain;
            _btnBack.Click += delegate { if (_step > 1) ShowStep(_step - 1); };
            _navBar.Controls.Add(_btnBack);

            _btnCancel = new FlatButton();
            _btnCancel.Text = "Cancelar";
            _btnCancel.Size = new Size(105, 36);
            _btnCancel.Location = new Point(137, 14);
            _btnCancel.BackColor = BtnGray;
            _btnCancel.ForeColor = TextMain;
            _btnCancel.Click += delegate { Close(); };
            _navBar.Controls.Add(_btnCancel);

            _btnNext = new FlatButton();
            _btnNext.Text = "Avançar >";
            _btnNext.Size = new Size(135, 36);
            _btnNext.Location = new Point(483, 14);
            _btnNext.BackColor = AccentBlue;
            _btnNext.ForeColor = Color.White;
            _btnNext.Click += OnNextClick;
            _navBar.Controls.Add(_btnNext);

            _pageContainer = new Panel();
            _pageContainer.Dock = DockStyle.Fill;
            _pageContainer.Padding = new Padding(32, 26, 32, 16);
            _pageContainer.BackColor = BgColor;

            Controls.Add(_pageContainer);
            Controls.Add(_navBar);
        }

        private void OnNextClick(object sender, EventArgs e)
        {
            if (_step == 2 && _txtDir != null)
            {
                string entered = _txtDir.Text.Trim();
                if (!string.IsNullOrEmpty(entered))
                {
                    _targetDir = entered;
                }
            }
            else if (_step == 3)
            {
                if (_chkDesktop != null) _desktopShortcut = _chkDesktop.Checked;
                if (_chkStart != null) _startShortcut = _chkStart.Checked;
                if (_chkPreserve != null) _preserveData = _chkPreserve.Checked;
            }
            else if (_step == 5)
            {
                FinishAndLaunch();
                return;
            }

            if (_step < 5)
            {
                ShowStep(_step + 1);
            }
        }

        private void ShowStep(int step)
        {
            _step = step;
            _pageContainer.Controls.Clear();

            if (step == 1) RenderWelcome();
            else if (step == 2) RenderDirectory();
            else if (step == 3) RenderOptions();
            else if (step == 4) RenderInstalling();
            else if (step == 5) RenderFinished();
        }

        private void RenderWelcome()
        {
            _btnBack.Enabled = false;
            _btnCancel.Enabled = true;
            _btnNext.Enabled = true;
            _btnNext.Text = "Avançar >";
            _btnNext.BackColor = AccentBlue;

            Panel badge = new Panel();
            badge.Size = new Size(88, 26);
            badge.Location = new Point(276, 38);
            badge.BackColor = Color.FromArgb(30, 41, 59);
            Label lblBadge = new Label();
            lblBadge.Text = "PRO v1.2";
            lblBadge.Font = new Font("Segoe UI", 8.5f, FontStyle.Bold);
            lblBadge.ForeColor = Color.FromArgb(56, 189, 248);
            lblBadge.Dock = DockStyle.Fill;
            lblBadge.TextAlign = ContentAlignment.MiddleCenter;
            badge.Controls.Add(lblBadge);
            _pageContainer.Controls.Add(badge);

            Label title = new Label();
            title.Text = "Bem-vindo ao Assistente de Instalação\ndo OmniScribe AI";
            title.Font = new Font("Segoe UI", 17f, FontStyle.Bold);
            title.ForeColor = TextMain;
            title.Size = new Size(576, 70);
            title.Location = new Point(32, 78);
            title.TextAlign = ContentAlignment.MiddleCenter;
            _pageContainer.Controls.Add(title);

            Panel card = new Panel();
            card.Size = new Size(556, 185);
            card.Location = new Point(42, 162);
            card.BackColor = CardColor;

            Label desc = new Label();
            desc.Text =
                "O OmniScribe AI realiza transcrição incremental em tempo real e gera Atas Executivas\n" +
                "automáticas para reuniões (Google Meet, Teams, Zoom, YouTube ou microfone).\n\n" +
                "Recursos incluídos nesta versão:\n" +
                "  - Diarização multi-voz inteligente e suporte a falas sobrepostas\n" +
                "  - Rastreador em tempo real de cotas Free Tier e custos da API\n" +
                "  - Instalação rápida sem privilégios de administrador ou terminal";
            desc.Font = new Font("Segoe UI", 9.5f);
            desc.ForeColor = TextMuted;
            desc.Dock = DockStyle.Fill;
            desc.Padding = new Padding(20);
            card.Controls.Add(desc);
            _pageContainer.Controls.Add(card);
        }

        private void RenderDirectory()
        {
            _btnBack.Enabled = true;
            _btnCancel.Enabled = true;
            _btnNext.Enabled = true;
            _btnNext.Text = "Avançar >";
            _btnNext.BackColor = AccentBlue;

            Label title = new Label();
            title.Text = "Pasta de Instalação";
            title.Font = new Font("Segoe UI", 15f, FontStyle.Bold);
            title.ForeColor = TextMain;
            title.Location = new Point(32, 34);
            title.Size = new Size(576, 34);
            _pageContainer.Controls.Add(title);

            Label sub = new Label();
            sub.Text = "Escolha a pasta onde o OmniScribe AI será instalado em seu computador:";
            sub.Font = new Font("Segoe UI", 9.5f);
            sub.ForeColor = TextMuted;
            sub.Location = new Point(32, 74);
            sub.Size = new Size(576, 26);
            _pageContainer.Controls.Add(sub);

            _txtDir = new TextBox();
            _txtDir.Text = _targetDir;
            _txtDir.Font = new Font("Segoe UI", 10f);
            _txtDir.BackColor = InputBg;
            _txtDir.ForeColor = TextMain;
            _txtDir.BorderStyle = BorderStyle.FixedSingle;
            _txtDir.Location = new Point(32, 118);
            _txtDir.Size = new Size(454, 30);
            _pageContainer.Controls.Add(_txtDir);

            FlatButton btnBrowse = new FlatButton();
            btnBrowse.Text = "Procurar...";
            btnBrowse.BackColor = BtnGray;
            btnBrowse.ForeColor = TextMain;
            btnBrowse.Size = new Size(110, 30);
            btnBrowse.Location = new Point(496, 117);
            btnBrowse.Click += delegate
            {
                using (FolderBrowserDialog dlg = new FolderBrowserDialog())
                {
                    dlg.Description = "Selecione a pasta de destino para o OmniScribe AI";
                    dlg.SelectedPath = _txtDir.Text;
                    if (dlg.ShowDialog(this) == DialogResult.OK && !string.IsNullOrEmpty(dlg.SelectedPath))
                    {
                        string chosen = dlg.SelectedPath;
                        if (!chosen.EndsWith("OmniScribe AI", StringComparison.OrdinalIgnoreCase))
                        {
                            chosen = Path.Combine(chosen, "OmniScribe AI");
                        }
                        _targetDir = chosen;
                        _txtDir.Text = chosen;
                    }
                }
            };
            _pageContainer.Controls.Add(btnBrowse);

            Label info = new Label();
            info.Text = "Espaço em disco necessário: ~125 MB\nNão requer permissão de administrador (instalação segura no perfil do usuário).";
            info.Font = new Font("Segoe UI", 9f);
            info.ForeColor = Color.FromArgb(100, 116, 139);
            info.Location = new Point(32, 170);
            info.Size = new Size(576, 50);
            _pageContainer.Controls.Add(info);
        }

        private void RenderOptions()
        {
            _btnBack.Enabled = true;
            _btnCancel.Enabled = true;
            _btnNext.Enabled = true;
            _btnNext.Text = "Instalar Agora";
            _btnNext.BackColor = AccentGreen;

            Label title = new Label();
            title.Text = "Configurações e Atalhos";
            title.Font = new Font("Segoe UI", 15f, FontStyle.Bold);
            title.ForeColor = TextMain;
            title.Location = new Point(32, 34);
            title.Size = new Size(576, 34);
            _pageContainer.Controls.Add(title);

            Label sub = new Label();
            sub.Text = "Selecione as opções adicionais para concluir a configuração:";
            sub.Font = new Font("Segoe UI", 9.5f);
            sub.ForeColor = TextMuted;
            sub.Location = new Point(32, 74);
            sub.Size = new Size(576, 26);
            _pageContainer.Controls.Add(sub);

            Panel card = new Panel();
            card.Location = new Point(32, 118);
            card.Size = new Size(576, 175);
            card.BackColor = CardColor;

            _chkDesktop = new CheckBox();
            _chkDesktop.Text = "Criar atalho na Área de Trabalho (Desktop)";
            _chkDesktop.Checked = _desktopShortcut;
            _chkDesktop.Font = new Font("Segoe UI", 10f);
            _chkDesktop.ForeColor = TextMain;
            _chkDesktop.Location = new Point(22, 24);
            _chkDesktop.Size = new Size(520, 30);
            card.Controls.Add(_chkDesktop);

            _chkStart = new CheckBox();
            _chkStart.Text = "Criar atalho no Menu Iniciar do Windows";
            _chkStart.Checked = _startShortcut;
            _chkStart.Font = new Font("Segoe UI", 10f);
            _chkStart.ForeColor = TextMain;
            _chkStart.Location = new Point(22, 68);
            _chkStart.Size = new Size(520, 30);
            card.Controls.Add(_chkStart);

            _chkPreserve = new CheckBox();
            _chkPreserve.Text = "Preservar atas, gravações anteriores e chave da API (.env) existentes";
            _chkPreserve.Checked = _preserveData;
            _chkPreserve.Font = new Font("Segoe UI", 10f);
            _chkPreserve.ForeColor = TextMain;
            _chkPreserve.Location = new Point(22, 112);
            _chkPreserve.Size = new Size(520, 30);
            card.Controls.Add(_chkPreserve);

            _pageContainer.Controls.Add(card);
        }

        private void RenderInstalling()
        {
            _btnBack.Enabled = false;
            _btnCancel.Enabled = false;
            _btnNext.Enabled = false;
            _btnNext.BackColor = BtnGray;

            Label title = new Label();
            title.Text = "Instalando o OmniScribe AI...";
            title.Font = new Font("Segoe UI", 15f, FontStyle.Bold);
            title.ForeColor = TextMain;
            title.Location = new Point(32, 70);
            title.Size = new Size(576, 36);
            _pageContainer.Controls.Add(title);

            _lblStatus = new Label();
            _lblStatus.Text = "Preparando ambiente...";
            _lblStatus.Font = new Font("Segoe UI", 9.5f);
            _lblStatus.ForeColor = TextMuted;
            _lblStatus.Location = new Point(32, 124);
            _lblStatus.Size = new Size(576, 28);
            _pageContainer.Controls.Add(_lblStatus);

            _progressBar = new ModernProgressBar();
            _progressBar.Location = new Point(32, 162);
            _progressBar.Size = new Size(576, 18);
            _progressBar.Value = 0.05;
            _pageContainer.Controls.Add(_progressBar);

            Thread worker = new Thread(RunInstallation);
            worker.IsBackground = true;
            worker.Start();
        }

        private void RenderFinished()
        {
            _btnBack.Enabled = false;
            _btnCancel.Enabled = false;
            _btnNext.Enabled = true;
            _btnNext.Text = "Concluir";
            _btnNext.BackColor = AccentGreen;

            Label title = new Label();
            title.Text = "Instalação Concluída com Sucesso!";
            title.Font = new Font("Segoe UI", 16.5f, FontStyle.Bold);
            title.ForeColor = AccentGreen;
            title.Location = new Point(32, 54);
            title.Size = new Size(576, 40);
            title.TextAlign = ContentAlignment.MiddleCenter;
            _pageContainer.Controls.Add(title);

            Panel card = new Panel();
            card.Location = new Point(42, 114);
            card.Size = new Size(556, 140);
            card.BackColor = CardColor;

            Label desc = new Label();
            desc.Text =
                "O OmniScribe AI foi instalado com sucesso em:\n" +
                _targetDir + "\n\n" +
                "Seus atalhos foram configurados e o software está pronto para uso imediato.";
            desc.Font = new Font("Segoe UI", 9.5f);
            desc.ForeColor = TextMuted;
            desc.Dock = DockStyle.Fill;
            desc.TextAlign = ContentAlignment.MiddleCenter;
            card.Controls.Add(desc);
            _pageContainer.Controls.Add(card);

            _chkLaunch = new CheckBox();
            _chkLaunch.Text = "Iniciar o OmniScribe AI agora";
            _chkLaunch.Checked = _launchNow;
            _chkLaunch.Font = new Font("Segoe UI", 10.5f, FontStyle.Bold);
            _chkLaunch.ForeColor = TextMain;
            _chkLaunch.Location = new Point(210, 282);
            _chkLaunch.Size = new Size(260, 32);
            _pageContainer.Controls.Add(_chkLaunch);
        }

        private void UpdateProgress(double value, string message)
        {
            if (IsDisposed) return;
            try
            {
                BeginInvoke((Action)delegate
                {
                    if (_progressBar != null && !_progressBar.IsDisposed) _progressBar.Value = value;
                    if (_lblStatus != null && !_lblStatus.IsDisposed) _lblStatus.Text = message;
                });
            }
            catch
            {
            }
        }

        private void ShowInstallError(string errorMessage)
        {
            if (IsDisposed) return;
            try
            {
                BeginInvoke((Action)delegate
                {
                    _pageContainer.Controls.Clear();
                    Label errLbl = new Label();
                    errLbl.Text = "Ocorreu um erro durante a instalação:\n\n" + errorMessage;
                    errLbl.Font = new Font("Segoe UI", 10f);
                    errLbl.ForeColor = Color.FromArgb(239, 68, 68);
                    errLbl.Location = new Point(32, 60);
                    errLbl.Size = new Size(576, 240);
                    errLbl.TextAlign = ContentAlignment.MiddleCenter;
                    _pageContainer.Controls.Add(errLbl);

                    _btnCancel.Text = "Fechar";
                    _btnCancel.Enabled = true;
                });
            }
            catch
            {
            }
        }

        private void RunInstallation()
        {
            try
            {
                UpdateProgress(0.08, "Fechando instâncias em execução...");
                RunHiddenProcess("taskkill.exe", "/f /im OmniScribe.exe");
                Thread.Sleep(150);

                string fullTargetDir = Path.GetFullPath(_targetDir);
                string parentDir = Path.GetDirectoryName(fullTargetDir) ?? fullTargetDir;
                string backupAtas = Path.Combine(parentDir, "_omniscribe_atas_backup");
                string backupEnv = Path.Combine(parentDir, "_omniscribe_env_backup");
                bool hasBackupAtas = false;
                bool hasBackupEnv = false;

                if (_preserveData && Directory.Exists(fullTargetDir))
                {
                    UpdateProgress(0.15, "Preservando suas atas e configurações (.env)...");
                    string oldAtas = Path.Combine(fullTargetDir, "atas_reunioes");
                    string oldEnv = Path.Combine(fullTargetDir, ".env");

                    if (Directory.Exists(oldAtas))
                    {
                        if (Directory.Exists(backupAtas)) Directory.Delete(backupAtas, true);
                        CopyDirectory(oldAtas, backupAtas);
                        hasBackupAtas = true;
                    }
                    if (File.Exists(oldEnv))
                    {
                        File.Copy(oldEnv, backupEnv, true);
                        hasBackupEnv = true;
                    }
                }

                Directory.CreateDirectory(fullTargetDir);

                bool extracted = TryExtractEmbeddedPayload(fullTargetDir);
                if (!extracted)
                {
                    string exeDir = Path.GetDirectoryName(Application.ExecutablePath) ?? "";
                    string externalZip = Path.Combine(exeDir, "app_data.zip");
                    if (!File.Exists(externalZip))
                    {
                        externalZip = Path.Combine(exeDir, "dist", "app_data.zip");
                    }

                    if (File.Exists(externalZip))
                    {
                        using (FileStream fs = new FileStream(externalZip, FileMode.Open, FileAccess.Read, FileShare.Read))
                        {
                            ExtractZipStream(fs, fullTargetDir);
                        }
                    }
                    else
                    {
                        string externalDir = Path.Combine(exeDir, "dist", "OmniScribe");
                        if (Directory.Exists(externalDir))
                        {
                            UpdateProgress(0.45, "Copiando arquivos do OmniScribe AI...");
                            CopyDirectory(externalDir, fullTargetDir);
                        }
                        else
                        {
                            throw new FileNotFoundException("Pacote de instalação não encontrado no executável.");
                        }
                    }
                }

                UpdateProgress(0.84, "Configurando variáveis de ambiente...");
                string targetEnv = Path.Combine(fullTargetDir, ".env");
                string targetExample = Path.Combine(fullTargetDir, ".env.example");
                if (hasBackupEnv && File.Exists(backupEnv))
                {
                    File.Copy(backupEnv, targetEnv, true);
                    File.Delete(backupEnv);
                }
                else if (!File.Exists(targetEnv) && File.Exists(targetExample))
                {
                    File.Copy(targetExample, targetEnv, false);
                }

                if (hasBackupAtas && Directory.Exists(backupAtas))
                {
                    string destAtas = Path.Combine(fullTargetDir, "atas_reunioes");
                    CopyDirectory(backupAtas, destAtas);
                    Directory.Delete(backupAtas, true);
                }

                UpdateProgress(0.90, "Registrando desinstalador...");
                WriteUninstaller(fullTargetDir);

                UpdateProgress(0.95, "Criando atalhos no Windows...");
                string exePath = Path.Combine(fullTargetDir, "OmniScribe.exe");
                if (_desktopShortcut)
                {
                    string desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
                    CreateShortcut(exePath, Path.Combine(desktop, "OmniScribe AI.lnk"), fullTargetDir);
                }
                if (_startShortcut)
                {
                    string startMenu = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
                    CreateShortcut(exePath, Path.Combine(startMenu, "OmniScribe AI.lnk"), fullTargetDir);
                }

                UpdateProgress(1.0, "Instalação concluída!");
                Thread.Sleep(250);
                BeginInvoke((Action)delegate { ShowStep(5); });
            }
            catch (Exception ex)
            {
                ShowInstallError(ex.Message);
            }
        }

        private bool TryExtractEmbeddedPayload(string targetDir)
        {
            string selfPath = Application.ExecutablePath;
            using (FileStream fs = new FileStream(selfPath, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                if (fs.Length < 16) return false;
                fs.Seek(-16, SeekOrigin.End);
                byte[] footer = new byte[16];
                int read = fs.Read(footer, 0, 16);
                if (read < 16) return false;

                string magic = Encoding.ASCII.GetString(footer, 8, 8);
                if (magic != "OMNISCRB") return false;

                long payloadSize = BitConverter.ToInt64(footer, 0);
                long payloadStart = fs.Length - 16 - payloadSize;
                if (payloadSize <= 0 || payloadStart < 0) return false;

                using (SubStream sub = new SubStream(fs, payloadStart, payloadSize))
                {
                    ExtractZipStream(sub, targetDir);
                }
                return true;
            }
        }

        private void ExtractZipStream(Stream zipStream, string targetDir)
        {
            string fullTargetRoot = Path.GetFullPath(targetDir);
            if (!fullTargetRoot.EndsWith(Path.DirectorySeparatorChar.ToString()))
            {
                fullTargetRoot += Path.DirectorySeparatorChar;
            }

            using (ZipArchive archive = new ZipArchive(zipStream, ZipArchiveMode.Read))
            {
                int total = archive.Entries.Count;
                int count = 0;

                foreach (ZipArchiveEntry entry in archive.Entries)
                {
                    count++;
                    string relativePath = entry.FullName.Replace('/', Path.DirectorySeparatorChar);
                    string destinationPath = Path.GetFullPath(Path.Combine(fullTargetRoot, relativePath));

                    if (!destinationPath.StartsWith(fullTargetRoot, StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }

                    if (string.IsNullOrEmpty(entry.Name))
                    {
                        Directory.CreateDirectory(destinationPath);
                        continue;
                    }

                    string dir = Path.GetDirectoryName(destinationPath);
                    if (!string.IsNullOrEmpty(dir))
                    {
                        Directory.CreateDirectory(dir);
                    }

                    using (Stream entryStream = entry.Open())
                    using (FileStream outStream = new FileStream(destinationPath, FileMode.Create, FileAccess.Write, FileShare.None))
                    {
                        entryStream.CopyTo(outStream);
                    }

                    if (count % 40 == 0 || count == total)
                    {
                        double ratio = (double)count / Math.Max(1, total);
                        double prog = 0.20 + (0.62 * ratio);
                        UpdateProgress(prog, string.Format("Extraindo: {0} ({1}%)", entry.Name, (int)(ratio * 100)));
                    }
                }
            }
        }

        private static void CopyDirectory(string sourceDir, string destDir)
        {
            Directory.CreateDirectory(destDir);
            foreach (string file in Directory.GetFiles(sourceDir))
            {
                string destFile = Path.Combine(destDir, Path.GetFileName(file));
                File.Copy(file, destFile, true);
            }
            foreach (string subDir in Directory.GetDirectories(sourceDir))
            {
                string destSub = Path.Combine(destDir, Path.GetFileName(subDir));
                CopyDirectory(subDir, destSub);
            }
        }

        private static void CreateShortcut(string targetExe, string shortcutPath, string workingDir)
        {
            try
            {
                string dir = Path.GetDirectoryName(shortcutPath);
                if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);

                string safeShortcut = shortcutPath.Replace("'", "''");
                string safeTarget = targetExe.Replace("'", "''");
                string safeWork = workingDir.Replace("'", "''");

                string psCmd = string.Format(
                    "$ws = New-Object -ComObject WScript.Shell; " +
                    "$s = $ws.CreateShortcut('{0}'); " +
                    "$s.TargetPath = '{1}'; " +
                    "$s.WorkingDirectory = '{2}'; " +
                    "$s.Description = 'OmniScribe AI'; " +
                    "$s.Save()",
                    safeShortcut, safeTarget, safeWork);

                RunHiddenProcess("powershell.exe", "-NoProfile -ExecutionPolicy Bypass -Command \"" + psCmd + "\"");
            }
            catch
            {
            }
        }

        private static void WriteUninstaller(string targetDir)
        {
            try
            {
                string uninstPath = Path.Combine(targetDir, "uninstall.bat");
                string script =
                    "@echo off\r\n" +
                    "chcp 65001 >nul\r\n" +
                    "title Desinstalador do OmniScribe AI\r\n" +
                    "echo ===============================================================================\r\n" +
                    "echo                   Desinstalador do OmniScribe AI\r\n" +
                    "echo ===============================================================================\r\n" +
                    "echo.\r\n" +
                    "set /p CONFIRM=\"Deseja remover o OmniScribe AI deste computador? (S/N): \"\r\n" +
                    "if /i not \"%CONFIRM%\"==\"S\" exit /b\r\n" +
                    "taskkill /f /im OmniScribe.exe >nul 2>&1\r\n" +
                    "del /f /q \"%USERPROFILE%\\Desktop\\OmniScribe AI.lnk\" >nul 2>&1\r\n" +
                    "del /f /q \"%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\OmniScribe AI.lnk\" >nul 2>&1\r\n" +
                    "cd ..\r\n" +
                    "set \"APP_DIR=%~dp0\"\r\n" +
                    "rmdir /s /q \"%APP_DIR%\" >nul 2>&1\r\n" +
                    "echo OmniScribe AI desinstalado com sucesso!\r\n" +
                    "pause\r\n";

                File.WriteAllText(uninstPath, script, new UTF8Encoding(false));
            }
            catch
            {
            }
        }

        private static void RunHiddenProcess(string fileName, string args)
        {
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo();
                psi.FileName = fileName;
                psi.Arguments = args;
                psi.CreateNoWindow = true;
                psi.UseShellExecute = false;
                psi.WindowStyle = ProcessWindowStyle.Hidden;
                using (Process p = Process.Start(psi))
                {
                    if (p != null) p.WaitForExit(8000);
                }
            }
            catch
            {
            }
        }

        private void FinishAndLaunch()
        {
            if (_chkLaunch != null && _chkLaunch.Checked)
            {
                string exePath = Path.Combine(_targetDir, "OmniScribe.exe");
                if (File.Exists(exePath))
                {
                    try
                    {
                        ProcessStartInfo psi = new ProcessStartInfo();
                        psi.FileName = exePath;
                        psi.WorkingDirectory = _targetDir;
                        psi.UseShellExecute = true;
                        Process.Start(psi);
                    }
                    catch
                    {
                    }
                }
            }
            Close();
        }

        [STAThread]
        public static void Main()
        {
            NativeMethods.SetProcessDPIAware();
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new InstallerForm());
        }
    }
}

