#!/usr/bin/env python3
"""
One-step installer for optional Local Whisper AI Subtitles.
Downloads:
  1. Standalone FFmpeg binary (via imageio-ffmpeg if system ffmpeg isn't installed)
  2. whisper.cpp CPU binaries (OpenBLAS x64 on Windows / x64 on Linux)
  3. Quantized English Whisper models:
       - ggml-tiny.en-q5_1.bin (~31 MB, "AI Fast")
       - ggml-base.en-q5_1.bin (~57 MB, "AI Accurate")

Usage:
  python setup_ai.py
"""

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(BASE_DIR, "tools")
WHISPER_DIR = os.path.join(TOOLS_DIR, "whisper")
MODELS_DIR = os.path.join(TOOLS_DIR, "models")

WHISPER_WIN_X64_URL = (
    "https://github.com/ggml-org/whisper.cpp/releases/download/b5907/whisper-blas-bin-x64.zip"
)
WHISPER_LINUX_X64_URL = (
    "https://github.com/ggml-org/whisper.cpp/releases/download/b5907/whisper-bin-ubuntu-x64.tar.gz"
)
MODELS = [
    (
        "ggml-tiny.en-q5_1.bin",
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en-q5_1.bin",
    ),
    (
        "ggml-base.en-q5_1.bin",
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en-q5_1.bin",
    ),
]


def download_with_progress(url, dest_path, label):
    tmp_path = dest_path + ".part"
    print(f"Downloading {label} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp_path, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total > 0:
                pct = min(100.0, done * 100.0 / total)
                mb = done / (1024 * 1024)
                tot_mb = total / (1024 * 1024)
                sys.stdout.write(f"\r  {mb:5.1f} / {tot_mb:5.1f} MB ({pct:5.1f}%)")
                sys.stdout.flush()
    if total > 0:
        sys.stdout.write("\n")
    os.replace(tmp_path, dest_path)


def setup_ffmpeg():
    is_win = os.name == "nt"
    local_ff = os.path.join(TOOLS_DIR, "ffmpeg.exe" if is_win else "ffmpeg")
    if os.path.isfile(local_ff):
        print(f"[OK] FFmpeg already present: {local_ff}")
        return
    sys_ff = shutil.which("ffmpeg")
    if sys_ff:
        print(f"[OK] System FFmpeg detected: {sys_ff}")
        return

    print("Installing standalone FFmpeg via imageio-ffmpeg ...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "imageio-ffmpeg"])
    import imageio_ffmpeg  # type: ignore

    src = imageio_ffmpeg.get_ffmpeg_exe()
    shutil.copy2(src, local_ff)
    if not is_win:
        os.chmod(local_ff, 0o755)
    print(f"[OK] FFmpeg installed to {local_ff}")


def setup_whisper_cli():
    is_win = os.name == "nt"
    local_cli = os.path.join(WHISPER_DIR, "whisper-cli.exe" if is_win else "whisper-cli")
    if os.path.isfile(local_cli):
        print(f"[OK] whisper-cli already present: {local_cli}")
        return
    sys_cli = shutil.which("whisper-cli")
    if sys_cli:
        print(f"[OK] System whisper-cli detected: {sys_cli}")
        return

    arch = platform.machine().lower()
    sys_name = platform.system().lower()

    if is_win and arch in ("amd64", "x86_64"):
        zip_path = os.path.join(TOOLS_DIR, "whisper-blas-x64.zip")
        download_with_progress(WHISPER_WIN_X64_URL, zip_path, "whisper.cpp Windows OpenBLAS x64")
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                fname = os.path.basename(member)
                if fname.lower().endswith((".exe", ".dll")):
                    with zf.open(member) as src, open(os.path.join(WHISPER_DIR, fname), "wb") as dst:
                        shutil.copyfileobj(src, dst)
        os.remove(zip_path)
        print(f"[OK] whisper-cli installed to {local_cli}")
    elif sys_name == "linux" and arch in ("amd64", "x86_64"):
        tar_path = os.path.join(TOOLS_DIR, "whisper-linux-x64.tar.gz")
        download_with_progress(WHISPER_LINUX_X64_URL, tar_path, "whisper.cpp Linux x64")
        with tarfile.open(tar_path, "r:gz") as tf:
            for member in tf.getmembers():
                if member.isfile():
                    fname = os.path.basename(member.name)
                    dest = os.path.join(WHISPER_DIR, fname)
                    src = tf.extractfile(member)
                    if src:
                        with src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        if "whisper" in fname or fname.endswith(".so"):
                            os.chmod(dest, 0o755)
        os.remove(tar_path)
        print(f"[OK] whisper-cli installed to {local_cli}")
    else:
        print(
            "[!] Prebuilt whisper.cpp auto-download is for Windows/Linux x64.\n"
            "    On macOS, run:  brew install whisper-cpp ffmpeg\n"
            "    (The server automatically detects system-installed whisper-cli & ffmpeg.)"
        )


def setup_models():
    for fname, url in MODELS:
        dest = os.path.join(MODELS_DIR, fname)
        if os.path.isfile(dest) and os.path.getsize(dest) > 1_000_000:
            mb = os.path.getsize(dest) / (1024 * 1024)
            print(f"[OK] Model already present: {fname} ({mb:.1f} MB)")
            continue
        download_with_progress(url, dest, fname)
        print(f"[OK] Model ready: {fname}")


def main():
    os.makedirs(WHISPER_DIR, exist_ok=True)
    os.makedirs(MODELS_DIR, exist_ok=True)
    print("=== Setting up Local Whisper AI Subtitles ===\n")
    setup_ffmpeg()
    setup_whisper_cli()
    setup_models()
    print("\nAll set! Restart server.py (if needed) and click 'CC' in the video player.")


if __name__ == "__main__":
    main()
