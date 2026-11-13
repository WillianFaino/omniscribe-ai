import os
import sys
import time
import zipfile
from pathlib import Path

def create_app_package(source_dir: Path, output_zip: Path):
    source_dir = Path(source_dir).resolve()
    output_zip = Path(output_zip).resolve()
    
    if not source_dir.exists():
        print(f"[ERRO] Pasta de origem não existe: {source_dir}")
        sys.exit(1)
        
    print(f"Compactando {source_dir} -> {output_zip}...")
    t0 = time.time()
    
    # Usa ZIP_DEFLATED com compresslevel=1 para velocidade máxima de escrita e leitura
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        file_count = 0
        for root, _, files in os.walk(source_dir):
            for file in files:
                file_path = Path(root) / file
                arcname = file_path.relative_to(source_dir)
                zf.write(file_path, arcname)
                file_count += 1
                
    elapsed = time.time() - t0
    size_mb = output_zip.stat().st_size / (1024 * 1024)
    print(f"[OK] {file_count} arquivos compactados em {elapsed:.2f}s! Tamanho final: {size_mb:.1f} MB")

if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "dist/OmniScribe"
    out = sys.argv[2] if len(sys.argv) > 2 else "dist/app_data.zip"
    create_app_package(Path(src), Path(out))

