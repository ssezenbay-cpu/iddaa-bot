"""Telegram'dan güncelleme: zip'i doğrula, testleri çalıştır, yedekle, kur, yeniden başlat.

Akış: kullanıcı iddaa-bot*.zip dosyasını bota gönderir -> geçici klasöre açılır -> gerekiyorsa
paketler kurulur -> testler (temiz ortamda) çalışır -> geçerse mevcut kod yedeklenir ve yenisi
kopyalanır -> süreç kapanır, systemd botu yeni kodla başlatır. Testler geçmezse hiçbir şey değişmez.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from .config import BASE_DIR, DATA_DIR

WORK = DATA_DIR / "update"
BACKUPS = DATA_DIR / "backup"
MARKER = WORK / "just_updated.txt"
CODE_DIRS = ("iddaabot", "tests", "deploy")
CODE_FILES = ("requirements.txt", "KURULUM.md", ".env.example")
MAX_SIZE = 5 * 1024 * 1024


class UpdateError(Exception):
    pass


def _safe_extract(zpath: Path, dest: Path) -> Path:
    with zipfile.ZipFile(zpath) as z:
        total = 0
        for info in z.infolist():
            p = Path(info.filename)
            if p.is_absolute() or ".." in p.parts:
                raise UpdateError(f"Güvensiz dosya yolu: {info.filename}")
            total += info.file_size
            if total > 50 * 1024 * 1024:
                raise UpdateError("Zip açılınca çok büyük.")
        z.extractall(dest)
    roots = [d for d in dest.iterdir() if d.is_dir() and (d / "iddaabot").is_dir()]
    if (dest / "iddaabot").is_dir():
        return dest
    if len(roots) != 1:
        raise UpdateError("Zip içinde iddaabot klasörü bulunamadı (doğru zip mi?).")
    return roots[0]


def _version(root: Path) -> str:
    try:
        for line in (root / "iddaabot" / "__init__.py").read_text(encoding="utf-8").splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return "?"


def current_version() -> str:
    return _version(BASE_DIR)


def _clean_env(tmp_data: Path) -> dict:
    """Testler kullanıcının .env ayarlarından etkilenmesin diye temiz ortam."""
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ")}
    keep["DATA_DIR"] = str(tmp_data)
    keep["IDDAABOT_NESTED"] = "1"   # güncelleme testi kendi içinde tekrar güncelleme testi çalıştırmasın
    return keep


def _run(cmd: list[str], cwd: Path, env: dict, timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)[-1500:]
    except subprocess.TimeoutExpired:
        return 1, "zaman aşımı"


def apply_zip(zpath: Path) -> tuple[str, str]:
    """Başarılıysa (yeni sürüm, özet) döndürür; hata olursa UpdateError (mevcut kod değişmez)."""
    if zpath.stat().st_size > MAX_SIZE:
        raise UpdateError("Dosya çok büyük.")
    stage = WORK / "stage"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    try:
        root = _safe_extract(zpath, stage)
    except zipfile.BadZipFile as e:
        raise UpdateError("Bu geçerli bir zip dosyası değil.") from e
    new_ver = _version(root)

    # Paket listesi değiştiyse önce paketleri kur
    old_req = (BASE_DIR / "requirements.txt").read_text() if (BASE_DIR / "requirements.txt").exists() else ""
    new_req = (root / "requirements.txt").read_text() if (root / "requirements.txt").exists() else old_req
    if new_req.strip() != old_req.strip():
        code, out = _run([sys.executable, "-m", "pip", "install", "-q", "-r", str(root / "requirements.txt")],
                         root, dict(os.environ), 600)
        if code != 0:
            raise UpdateError("Paket kurulumu başarısız:\n" + out[-600:])

    # Testler: temiz ortamda, yeni kodla
    tmp_data = WORK / "testdata"
    shutil.rmtree(tmp_data, ignore_errors=True)
    tmp_data.mkdir(parents=True)
    code, out = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/"],
                     root, _clean_env(tmp_data), 600)
    if code != 0:
        raise UpdateError("Testler geçmedi, güncelleme uygulanmadı:\n" + out[-800:])
    summary = next((ln for ln in reversed(out.splitlines()) if "passed" in ln), "testler geçti")

    # Yedekle ve kur
    bdir = BACKUPS / datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir.mkdir(parents=True)
    for d in CODE_DIRS:
        if (BASE_DIR / d).exists():
            shutil.copytree(BASE_DIR / d, bdir / d)
    for f in CODE_FILES:
        if (BASE_DIR / f).exists():
            shutil.copy2(BASE_DIR / f, bdir / f)
    _install_from(root)
    # en fazla 5 yedek tut
    for old in sorted(BACKUPS.iterdir())[:-5]:
        shutil.rmtree(old, ignore_errors=True)
    MARKER.write_text(f"{new_ver}\n{summary}", encoding="utf-8")
    shutil.rmtree(stage, ignore_errors=True)
    return new_ver, summary


def _install_from(root: Path) -> None:
    for d in CODE_DIRS:
        if (root / d).exists():
            shutil.rmtree(BASE_DIR / d, ignore_errors=True)
            shutil.copytree(root / d, BASE_DIR / d)
    for f in CODE_FILES:
        if (root / f).exists():
            shutil.copy2(root / f, BASE_DIR / f)


def rollback() -> str | None:
    """Son yedeğe döner. Dönülen sürümü döndürür; yedek yoksa None."""
    if not BACKUPS.exists():
        return None
    bs = sorted(BACKUPS.iterdir())
    if not bs:
        return None
    last = bs[-1]
    _install_from(last)
    ver = _version(last)
    shutil.rmtree(last, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(f"{ver}\ngeri alındı", encoding="utf-8")
    return ver


def pop_marker() -> str | None:
    if MARKER.exists():
        txt = MARKER.read_text(encoding="utf-8")
        MARKER.unlink()
        return txt
    return None
