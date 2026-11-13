"""Empacotador de alta performance para o instalador nativo do OmniScribe AI.

1. Compacta os binários de dist/OmniScribe em dist/app_data.zip.
2. Compila src/installer_native.cs com o compilador nativo Win32/.NET (csc.exe) gerando um stub de ~30 KB.
3. Anexa app_data.zip ao final (EOF Overlay) do executável com assinatura de 16 bytes 'OMNISCRB',
   garantindo abertura instantânea (< 0.1s) sem extração prévia para %TEMP%.
"""

import os
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path

MAGIC_SIGNATURE = b"OMNISCRB"


def create_app_package(source_dir: Path, output_zip: Path) -> Path:
    source_dir = Path(source_dir).resolve()
    output_zip = Path(output_zip).resolve()

    if not source_dir.exists():
        print(f"[ERRO] Pasta de origem não existe: {source_dir}")
        sys.exit(1)

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    print(f"Compactando {source_dir} -> {output_zip}...")
    t0 = time.time()

    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        file_count = 0
        for root, dirs, files in os.walk(source_dir):
            # Evita empacotar pastas de runtime/logs locais dentro do instalador distribuível
            dirs[:] = [d for d in dirs if d not in ("atas_reunioes", "__pycache__")]
            for file in files:
                if file == ".env":
                    continue
                file_path = Path(root) / file
                arcname = file_path.relative_to(source_dir)
                zf.write(file_path, arcname)
                file_count += 1

    elapsed = time.time() - t0
    size_mb = output_zip.stat().st_size / (1024 * 1024)
    print(f"[OK] {file_count} arquivos compactados em {elapsed:.2f}s ({size_mb:.1f} MB)")
    return output_zip


def find_csc_compiler() -> Path:
    """Localiza o compilador C# nativo do Windows (.NET Framework 4.x)."""
    candidates = [
        Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
        Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe"),
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError("Compilador csc.exe (.NET Framework 4.x) não encontrado no Windows.")


def build_native_installer(root_dir: Path, zip_path: Path) -> Path:
    """Compila o stub WinForms nativo e acopla o payload ZIP no final (EOF)."""
    cs_source = root_dir / "src" / "installer_native.cs"
    if not cs_source.exists():
        raise FileNotFoundError(f"Arquivo fonte nativo não encontrado: {cs_source}")

    csc_exe = find_csc_compiler()
    stub_exe = root_dir / "dist" / "OmniScribe_Setup_Stub.exe"
    final_exe = root_dir / "dist" / "OmniScribe_Setup.exe"
    root_copy = root_dir / "OmniScribe_Setup.exe"

    cmd = [
        str(csc_exe),
        "/nologo",
        "/target:winexe",
        "/optimize+",
        "/codepage:65001",
        "/utf8output",
        "/r:System.Windows.Forms.dll",
        "/r:System.Drawing.dll",
        "/r:System.IO.Compression.dll",
        f"/out:{stub_exe}",
        str(cs_source),
    ]

    print(f"Compilando stub nativo com {csc_exe.name}...")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stdout)
        print(res.stderr)
        raise RuntimeError(f"Falha ao compilar installer_native.cs (código {res.returncode})")

    stub_size_kb = stub_exe.stat().st_size / 1024
    zip_size = zip_path.stat().st_size
    print(f"[OK] Stub nativo compilado ({stub_size_kb:.1f} KB). Anexando payload EOF ({zip_size / (1024 * 1024):.1f} MB)...")

    with open(stub_exe, "rb") as f_stub, open(zip_path, "rb") as f_zip, open(final_exe, "wb") as f_out:
        shutil.copyfileobj(f_stub, f_out, length=1024 * 1024)
        shutil.copyfileobj(f_zip, f_out, length=1024 * 1024)
        f_out.write(struct.pack("<Q8s", zip_size, MAGIC_SIGNATURE))

    try:
        stub_exe.unlink()
    except OSError:
        pass

    shutil.copy2(final_exe, root_copy)
    final_mb = final_exe.stat().st_size / (1024 * 1024)
    print(f"[OK] Instalador nativo gerado com sucesso: {final_exe} ({final_mb:.2f} MB)")
    print(f"[OK] Cópia pronta para uso em: {root_copy}")
    return final_exe


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "dist" / "OmniScribe"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else root / "dist" / "app_data.zip"

    zip_file = create_app_package(src, out)
    if sys.platform == "win32":
        build_native_installer(root, zip_file)
    else:
        print("[AVISO] Compilação nativa csc.exe disponível apenas no Windows. Apenas app_data.zip foi gerado.")
