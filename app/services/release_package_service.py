"""
Apply full-stack release packages (zip): dashboard (React build) + api (Python app).
Backups and rollback live under api/.release_backups/
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

log = logging.getLogger(__name__)

FORMAT_ID = "boondock-edge-release"
MAX_ZIP_BYTES = 200 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 1 * 1024 * 1024 * 1024
MAX_EXTRACTED_FILES = 50_000
BACKUP_RETENTION = 5

_STATE_NAME = "state.json"


def api_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def backups_dir() -> Path:
    d = api_root() / ".release_backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path() -> Path:
    return backups_dir() / _STATE_NAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_state() -> dict[str, Any]:
    p = state_path()
    if not p.is_file():
        return {"backups": [], "current": None, "format": FORMAT_ID}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"backups": [], "current": None, "format": FORMAT_ID}
        data.setdefault("backups", [])
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read release state: %s", e)
        return {"backups": [], "current": None, "format": FORMAT_ID}


def _save_state(data: dict[str, Any]) -> None:
    sp = state_path()
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def get_status() -> dict[str, Any]:
    st = _load_state()
    cur = st.get("current")
    return {
        "format": FORMAT_ID,
        "current": cur,
        "backups": st.get("backups", []),
    }


def _is_safe_rel(name: str) -> bool:
    if not name or name.strip() == "":
        return False
    parts = name.replace("\\", "/").split("/")
    for p in parts:
        if p in ("", ".", ".."):
            if p == "..":
                return False
    if parts[-1] == "..":
        return False
    if ".." in parts:
        return False
    return not Path(name).is_absolute() if not name.startswith(("/", "\\\\")) else False


def _validate_zip_info(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = zf.infolist()
    total_uncompressed = 0
    for i, m in enumerate(members):
        if i >= MAX_EXTRACTED_FILES:
            raise ValueError("Too many files in archive (zip bomb?)")
        if m.is_dir() or m.filename.endswith("/"):
            continue
        if not _is_safe_rel(m.filename):
            raise ValueError(f"Illegal path in archive: {m.filename!r}")
        if m.file_size < 0:
            raise ValueError("Invalid file size in archive")
        total_uncompressed += m.file_size
        if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("Uncompressed size of archive is too large")
    return members


def _safe_extract_zf(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for info in zf.infolist():
        name = info.filename
        if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
            raise ValueError("Absolute paths are not allowed in the archive")
        if not _is_safe_rel(name):
            raise ValueError(f"Path traversal or invalid name: {name!r}")
        target = (dest / name).resolve()
        try:
            target.relative_to(dest)
        except ValueError as e:
            raise ValueError("Path would escape destination directory (zip slip)") from e
    zf.extractall(str(dest))


def _read_manifest(extract: Path) -> dict[str, Any]:
    mpath = extract / "manifest.json"
    if not mpath.is_file():
        raise ValueError("manifest.json is missing in the archive (root of zip)")
    with open(mpath, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("manifest.json must be a JSON object")
    if raw.get("format") != FORMAT_ID:
        raise ValueError(
            f'Unsupported or missing "format" (expected "{FORMAT_ID}") in manifest.json'
        )
    return raw


def _sync_directory(src: Path, dst: Path) -> None:
    if not src.is_dir():
        raise ValueError(f"Expected directory: {src}")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _copy_file(src: Path, dst: Path) -> None:
    if not src.is_file():
        raise ValueError(f"Expected file: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _apply_manifest(extract: Path, manifest: dict[str, Any], br: Path) -> list[str]:
    log_lines: list[str] = []
    comp = manifest.get("components", {})
    if not isinstance(comp, dict):
        raise ValueError("manifest components must be an object")

    fe = comp.get("dashboard", {})
    if not isinstance(fe, dict):
        raise ValueError("components.dashboard must be an object")
    front_rel = fe.get("path", "dashboard")
    fsrc = extract / front_rel
    if not (fsrc / "index.html").is_file():
        raise ValueError("dashboard folder must contain index.html")
    fstat = fsrc / "static"
    fass = fsrc / "assets"
    if not fstat.is_dir() and not fass.is_dir():
        raise ValueError("dashboard folder must contain a static/ or assets/ directory")
    out_build = br / "build"
    if out_build.exists():
        shutil.rmtree(out_build)
    shutil.copytree(fsrc, out_build)
    log_lines.append("Applied dashboard to build/")

    be = comp.get("api", {})
    if not isinstance(be, dict):
        raise ValueError("components.api must be an object")
    back_rel = be.get("path", "api")
    bsrc = extract / back_rel
    ap = bsrc / "app"
    if not ap.is_dir():
        raise ValueError("api app/ directory is missing in package")
    out_app = br / "app"
    if out_app.exists():
        shutil.rmtree(out_app)
    shutil.copytree(ap, out_app)
    log_lines.append("Applied api app/")

    for fn in be.get("files", ["run.py", "config.py", "requirements.txt"]):
        p = bsrc / fn
        if not p.is_file():
            raise ValueError(f"Required api file missing: {back_rel}/{fn}")
        _copy_file(p, br / fn)
        log_lines.append(f"Applied {fn}")

    de = comp.get("docs", {})
    if isinstance(de, dict) and de.get("include", False) is not False:
        drel = de.get("path", "docs-build")
        dsrc = extract / drel
        if dsrc.is_dir():
            out_docs = br / "docs-build"
            if out_docs.exists():
                shutil.rmtree(out_docs)
            shutil.copytree(dsrc, out_docs)
            log_lines.append("Applied docs-build/")
        else:
            log_lines.append("docs expected but not present in archive; skipped")
    return log_lines


def _create_snapshot(backup_id: str, br: Path) -> Path:
    snap = backups_dir() / backup_id / "snapshot"
    snap.mkdir(parents=True, exist_ok=True)
    bdir = br / "build"
    if bdir.is_dir():
        shutil.copytree(bdir, snap / "build", dirs_exist_ok=True)
    adir = br / "app"
    if adir.is_dir():
        shutil.copytree(adir, snap / "app", dirs_exist_ok=True)
    for name in ("run.py", "config.py", "requirements.txt"):
        p = br / name
        if p.is_file():
            shutil.copy2(p, snap / name)
    ddocs = br / "docs-build"
    if ddocs.is_dir():
        shutil.copytree(ddocs, snap / "docs-build", dirs_exist_ok=True)
    return snap


def _restore_snapshot(snap: Path, br: Path) -> None:
    b_from = snap / "build"
    if b_from.is_dir():
        _sync_directory(b_from, br / "build")
    a_from = snap / "app"
    if a_from.is_dir():
        _sync_directory(a_from, br / "app")
    for name in ("run.py", "config.py", "requirements.txt"):
        p = snap / name
        if p.is_file():
            _copy_file(p, br / name)
    d_from = snap / "docs-build"
    d_to = br / "docs-build"
    if d_from.is_dir():
        if d_to.exists():
            shutil.rmtree(d_to)
        shutil.copytree(d_from, d_to)
    # If snapshot has no docs-build, leave the current site as-is


def _prune_backups() -> None:
    st = _load_state()
    bks = st.get("backups", [])
    if len(bks) <= BACKUP_RETENTION:
        return
    extra = bks[BACKUP_RETENTION:]
    st["backups"] = bks[:BACKUP_RETENTION]
    for entry in extra:
        bid = entry.get("id")
        if not bid:
            continue
        p = backups_dir() / str(bid)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    _save_state(st)


def _run_pip_if_requested(br: Path) -> Optional[Tuple[bool, str]]:
    req = br / "requirements.txt"
    if not req.is_file():
        return None
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req)],
            cwd=str(br),
            capture_output=True,
            text=True,
            timeout=600,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)


def apply_release_package(
    zip_data: bytes,
    *,
    install_dependencies: bool = False,
) -> dict[str, Any]:
    if len(zip_data) > MAX_ZIP_BYTES:
        raise ValueError(f"Archive too large (max {MAX_ZIP_BYTES} bytes)")

    br = api_root()
    with tempfile.TemporaryDirectory(prefix="bd_release_") as td:
        tdp = Path(td)
        zpath = tdp / "upload.zip"
        zpath.write_bytes(zip_data)

        with zipfile.ZipFile(zpath) as zf:
            if zf.testzip() is not None:
                raise ValueError("Corrupt zip (CRC mismatch)")
            _validate_zip_info(zf)
            extract = tdp / "ex"
            extract.mkdir()
            _safe_extract_zf(zf, extract)

        manifest = _read_manifest(extract)
        version = str(manifest.get("version", "unknown"))[:200]
        build_id = str(manifest.get("build_id", "unknown"))[:200]

        pre_snap_id = f"{_utc_now()[:10].replace('-', '')}-{_new_backup_suffix()}"
        _create_snapshot(pre_snap_id, br)
        st_pre = _load_state()
        pre_entry = {
            "id": pre_snap_id,
            "version": version,
            "build_id": build_id,
            "type": "pre_update",
            "label": f"Before upgrade to {version}",
            "created_at": _utc_now(),
        }
        st_pre["backups"] = [pre_entry, *[b for b in st_pre.get("backups", []) if b.get("id") != pre_snap_id]]
        st_pre["backups"] = st_pre["backups"][:20]
        _save_state(st_pre)

        try:
            lines = _apply_manifest(extract, manifest, br)
        except Exception:
            log.exception("Release apply failed, restoring pre-update snapshot")
            snap = backups_dir() / pre_snap_id / "snapshot"
            if snap.is_dir():
                _restore_snapshot(snap, br)
            st_r = _load_state()
            st_r["backups"] = [b for b in st_r.get("backups", []) if b.get("id") != pre_snap_id]
            _save_state(st_r)
            if (backups_dir() / pre_snap_id).is_dir():
                shutil.rmtree(backups_dir() / pre_snap_id, ignore_errors=True)
            raise

        pip_result = None
        if install_dependencies:
            pip_result = _run_pip_if_requested(br)

        st = _load_state()
        st["current"] = {
            "version": version,
            "build_id": build_id,
            "applied_at": _utc_now(),
            "backup_id": pre_snap_id,
        }
        _save_state(st)
        _prune_backups()

        return {
            "ok": True,
            "version": version,
            "build_id": build_id,
            "message": "Release applied. Restart the Boondock Edge service for all changes to take effect.",
            "log": lines,
            "pre_update_backup_id": pre_snap_id,
            "pip_install": pip_result,
        }


def _new_backup_suffix() -> str:
    return uuid.uuid4().hex[:10]


def rollback(backup_id: str) -> dict[str, Any]:
    if not backup_id or len(backup_id) > 120:
        raise ValueError("Invalid backup id")
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", backup_id):
        raise ValueError("Invalid backup id")
    bid = backup_id
    if ".." in bid or Path(bid).is_absolute():
        raise ValueError("Invalid backup id")
    root = backups_dir() / bid / "snapshot"
    if not root.is_dir():
        raise ValueError("Backup not found")
    br = api_root()
    st = _load_state()
    re_snap_id = f"rollback-{_new_backup_suffix()}"
    re_snap = _create_snapshot(re_snap_id, br)
    shutil.move(str(re_snap), str(backups_dir() / re_snap_id / "snapshot"))
    st["backups"] = [
        {
            "id": re_snap_id,
            "type": "pre_rollback",
            "label": f"State before rollback to {bid}",
            "created_at": _utc_now(),
        },
        *st.get("backups", []),
    ][:20]
    _save_state(st)
    try:
        _restore_snapshot(root, br)
    except Exception:
        re_path = backups_dir() / re_snap_id / "snapshot"
        if re_path.is_dir():
            _restore_snapshot(re_path, br)
        st2 = _load_state()
        st2["backups"] = [b for b in st2.get("backups", []) if b.get("id") != re_snap_id]
        _save_state(st2)
        if (backups_dir() / re_snap_id).is_dir():
            shutil.rmtree(backups_dir() / re_snap_id, ignore_errors=True)
        raise

    st2 = _load_state()
    st2["current"] = {
        "version": f"restored from backup {bid}",
        "build_id": bid,
        "applied_at": _utc_now(),
    }
    _save_state(st2)
    return {
        "ok": True,
        "message": f"Rolled back to backup {bid}. Restart the Boondock Edge service to load the restored code.",
    }
