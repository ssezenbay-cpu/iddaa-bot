import os
import shutil
import zipfile
from pathlib import Path

import pytest

from iddaabot import updater

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(os.getenv("IDDAABOT_NESTED") == "1", reason="iç içe test")


def _copy_project(dst: Path) -> None:
    for d in updater.CODE_DIRS:
        shutil.copytree(ROOT / d, dst / d, ignore=shutil.ignore_patterns("__pycache__"))
    for f in updater.CODE_FILES:
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, dst / f)


def _make_zip(src: Path, out: Path, version: str, break_tests: bool = False) -> Path:
    stage = out.parent / f"stage_{version}"
    _copy_project(stage / "iddaa-bot")
    (stage / "iddaa-bot" / "iddaabot" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    if break_tests:
        (stage / "iddaa-bot" / "tests" / "test_bozuk.py").write_text("def test_x():\n    assert False\n")
    with zipfile.ZipFile(out, "w") as z:
        for p in stage.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                z.write(p, p.relative_to(stage))
    return out


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    base = tmp_path / "base"
    _copy_project(base)
    (base / "iddaabot" / "__init__.py").write_text('__version__ = "eski"\n')
    (base / ".env").write_text("TELEGRAM_TOKEN=gizli\n")
    data = tmp_path / "data"
    monkeypatch.setattr(updater, "BASE_DIR", base)
    monkeypatch.setattr(updater, "WORK", data / "update")
    monkeypatch.setattr(updater, "BACKUPS", data / "backup")
    monkeypatch.setattr(updater, "MARKER", data / "update" / "just_updated.txt")
    return base, tmp_path


def test_update_and_rollback(sandbox):
    base, tmp = sandbox
    z = _make_zip(ROOT, tmp / "iddaa-bot-v99.zip", "99")
    ver, summary = updater.apply_zip(z)
    assert ver == "99" and "passed" in summary
    assert updater.current_version() == "99"
    assert (base / ".env").read_text() == "TELEGRAM_TOKEN=gizli\n"   # ayarlar korunur
    assert updater.pop_marker().startswith("99")
    assert updater.rollback() == "eski" and updater.current_version() == "eski"


def test_failing_update_changes_nothing(sandbox):
    base, tmp = sandbox
    z = _make_zip(ROOT, tmp / "iddaa-bot-bad.zip", "bad", break_tests=True)
    with pytest.raises(updater.UpdateError):
        updater.apply_zip(z)
    assert updater.current_version() == "eski"
    bad = tmp / "iddaa-bot-evil.zip"
    with zipfile.ZipFile(bad, "w") as zz:
        zz.writestr("../evil.txt", "x")
    with pytest.raises(updater.UpdateError):
        updater.apply_zip(bad)


def test_github_zipball_layout(sandbox):
    """GitHub zipball'ı 'sahip-depo-sha/' klasörüyle gelir; kurulum bunu tanımalı."""
    base, tmp = sandbox
    stage = tmp / "gh"
    _copy_project(stage / "ssezenbay-cpu-iddaa-bot-abc1234")
    (stage / "ssezenbay-cpu-iddaa-bot-abc1234" / "iddaabot" / "__init__.py").write_text('__version__ = "gh"\n')
    z = tmp / "github.zip"
    with zipfile.ZipFile(z, "w") as zz:
        for p in stage.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                zz.write(p, p.relative_to(stage))
    ver, _ = updater.apply_zip(z)
    assert ver == "gh" and updater.current_version() == "gh"
