from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
import re

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename


@dataclass(frozen=True)
class AddonInstallResult:
    addon_id: str
    target_dir: str
    checksum_sha256: str


class AddonInstallError(RuntimeError):
    pass


_ADDON_ID_RE = re.compile(r"^[a-z0-9_]{2,64}$")
_TEXT_EXTENSIONS = {".py", ".json", ".html", ".css", ".js", ".md", ".txt", ".svg"}
_MAX_FILES = 300
_MAX_SINGLE_FILE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_UNCOMPRESSED_BYTES = 8 * 1024 * 1024


def _dir_checksum(path: Path) -> str:
    h = hashlib.sha256()
    for file_path in sorted(path.rglob("*")):
        if not file_path.is_file():
            continue
        rel_path = file_path.relative_to(path)
        if "__pycache__" in rel_path.parts or file_path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        h.update(str(rel_path).encode("utf-8"))
        h.update(file_path.read_bytes())
    return h.hexdigest()


def _safe_zip_members(zf: zipfile.ZipFile) -> list[str]:
    names: list[str] = []
    total_uncompressed = 0
    file_count = 0
    for info in zf.infolist():
        name = info.filename
        # Normalize separators
        name = name.replace("\\", "/")
        if not name or name.startswith("/") or name.startswith("../") or "/../" in name:
            raise AddonInstallError("ZIP non valido: path non sicuro")
        if info.is_dir():
            names.append(name)
            continue
        # Disallow symlinks (best-effort)
        is_symlink = (info.external_attr >> 16) & 0o120000 == 0o120000
        if is_symlink:
            raise AddonInstallError("ZIP non valido: symlink non permessi")
        # Reject absolute paths inside ZIP
        parts = [p for p in name.split("/") if p]
        for part in parts:
            if part in ("..", "."):
                raise AddonInstallError("ZIP non valido: path traversal rilevato")
        ext = Path(name).suffix.lower()
        if ext and ext not in _TEXT_EXTENSIONS:
            raise AddonInstallError(f"ZIP non valido: file type non consentito ({ext})")
        if int(info.file_size or 0) > _MAX_SINGLE_FILE_BYTES:
            raise AddonInstallError("ZIP non valido: file troppo grande")
        total_uncompressed += int(info.file_size or 0)
        file_count += 1
        if file_count > _MAX_FILES:
            raise AddonInstallError("ZIP non valido: troppi file")
        if total_uncompressed > _MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise AddonInstallError("ZIP non valido: dimensione complessiva troppo grande")
        names.append(name)
    return names


def _root_folder(names: list[str]) -> str:
    tops = set()
    for n in names:
        parts = [p for p in n.split("/") if p]
        if not parts:
            continue
        tops.add(parts[0])
    if len(tops) != 1:
        raise AddonInstallError("ZIP non valido: deve contenere una sola cartella root")
    return next(iter(tops))


def install_addon_zip(upload: FileStorage, addons_root: str) -> AddonInstallResult:
    """Install a ZIP add-on into addons_root safely.

    Expected structure:
      <root>/__init__.py   (optional – created if missing)
      <root>/addon.py
      <root>/config.json
      <root>/visual.json
      <root>/... (templates, static, etc.)
    """

    if upload is None or not getattr(upload, "filename", ""):
        raise AddonInstallError("Nessun file caricato")
    filename = secure_filename(str(upload.filename))
    if not filename.lower().endswith(".zip"):
        raise AddonInstallError("Formato non supportato: serve un .zip")

    addons_root_path = Path(addons_root).resolve()
    addons_root_path.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="addon_install_") as tmpdir:
        tmp_zip_path = Path(tmpdir) / filename
        upload.save(tmp_zip_path)

        with zipfile.ZipFile(tmp_zip_path, "r") as zf:
            names = _safe_zip_members(zf)
            root = _root_folder(names)

            required = {f"{root}/addon.py", f"{root}/config.json", f"{root}/visual.json"}
            if not required.issubset(set(n.rstrip("/") for n in names)):
                raise AddonInstallError("ZIP non valido: mancano addon.py/config.json/visual.json")

            extract_dir = Path(tmpdir) / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)
            zf.extractall(extract_dir)

        src_dir = (Path(tmpdir) / "extract" / root).resolve()
        # Validate addon_id: only lowercase letters, digits, underscores
        addon_id = root.strip().lower()
        if not addon_id or not _ADDON_ID_RE.match(addon_id):
            raise AddonInstallError("Nome cartella add-on non valido (solo a-z, 0-9, _)")

        dst_dir = (addons_root_path / addon_id).resolve()
        if not str(dst_dir).startswith(str(addons_root_path)):
            raise AddonInstallError("Destinazione non valida")
        if dst_dir.exists():
            raise AddonInstallError(f"Add-on già presente: {addon_id}")

        shutil.copytree(src_dir, dst_dir)

        # Ensure __init__.py exists (required for Python import)
        init_file = dst_dir / "__init__.py"
        if not init_file.exists():
            init_file.write_text("", encoding="utf-8")

        # Ensure templates subfolder has no __init__.py (not needed, avoids confusion)
        # Basic permission hardening
        for p in dst_dir.rglob("*"):
            try:
                if p.is_file():
                    os.chmod(p, 0o644)
                elif p.is_dir():
                    os.chmod(p, 0o755)
            except Exception:
                pass

    return AddonInstallResult(addon_id=addon_id, target_dir=str(dst_dir), checksum_sha256=_dir_checksum(dst_dir))



def export_addon_zip(addon_id: str, *, addons_root: str = "addons") -> str:
    """Create a ZIP for a single add-on folder and return the zip path.

    The ZIP will contain exactly one top-level folder: <addon_id>/...
    """
    addon_id = (addon_id or "").strip()
    if not addon_id or "/" in addon_id or "\\" in addon_id or addon_id.startswith("."):
        raise AddonInstallError("Addon id non valido")
    root = Path(addons_root).resolve()
    src = (root / addon_id).resolve()
    if not str(src).startswith(str(root)) or not src.exists() or not src.is_dir():
        raise AddonInstallError("Add-on non trovato")

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"addon_export_{addon_id}_"))
    zip_path = tmp_dir / f"{addon_id}.zip"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src.rglob("*")):
            if p.is_dir():
                continue
            rel = p.relative_to(root).as_posix()  # includes addon_id/...
            zf.write(p, arcname=rel)
    return str(zip_path)


def uninstall_addon(addon_id: str, *, addons_root: str = "addons") -> str:
    """Remove a single add-on directory safely. Returns removed path."""
    addon_id = (addon_id or "").strip()
    if not addon_id or "/" in addon_id or "\\" in addon_id or addon_id.startswith("."):
        raise AddonInstallError("Addon id non valido")
    root = Path(addons_root).resolve()
    target = (root / addon_id).resolve()
    if not str(target).startswith(str(root)) or not target.exists() or not target.is_dir():
        raise AddonInstallError("Add-on non trovato")
    shutil.rmtree(target)
    return str(target)
