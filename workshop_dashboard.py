#!/usr/bin/env python3
"""
STS2 Steam Workshop 업로드 대시보드 (로컬 웹)

사용법:
    python workshop_dashboard.py
    -> http://127.0.0.1:8791 자동 안내 (브라우저에서 열기)

기능:
  * 게임 mods/ 폴더에 설치된 모드를 자동 스캔
  * 좌측 목록에서 모드 선택 -> 우측 상세 패널(썸네일/타이틀/설명 + 등록 검증 체크리스트)에서 업로드
  * 게임 mods/<id>/ (설치/검증된 내용물) 을 content 로 패키징
  * [Upload] (설치본 그대로) / [Build+Upload] (dotnet build -c Release 후)
  * ModUploader.exe 출력을 실시간 로그로 스트리밍 (SSE)
  * 모드별 워크스페이스(.workshop/<id>/) 에 workshop.json + mod_id.txt 영속 -> 갱신은 같은 워크샵 아이템으로

전제:
  * 업로드 시 Steam 클라이언트 실행 + 로그인 상태여야 함 (헤드리스 불가)
  * ModUploader.exe (+ steam_api64.dll + steam_appid.txt) 가 UPLOADER_DIR 에 있어야 함
"""
import os
import sys
import json
import time
import struct
import shutil
import threading
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------
# 설정 (환경변수로 override 가능)
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    p = os.path.join(HERE, "config.json")
    if os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception as e:
            print(f"[config.json parse failed] {e}")
    return {}


CONFIG = load_config()


def _steam_root():
    try:
        import winreg
        for hive, key, name in [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ]:
            try:
                with winreg.OpenKey(hive, key) as k:
                    val, _ = winreg.QueryValueEx(k, name)
                    if val and os.path.isdir(val):
                        return val
            except OSError:
                continue
    except Exception:
        pass
    return r"C:\Program Files (x86)\Steam"


def _find_game_path():
    p = os.environ.get("STS2_PATH") or CONFIG.get("game_path")
    if p and os.path.isdir(p):
        return p
    steam = _steam_root()
    libs = [os.path.join(steam, "steamapps")]
    vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(vdf):
        try:
            import re
            txt = open(vdf, encoding="utf-8", errors="replace").read()
            for m in re.finditer(r'"path"\s*"([^"]+)"', txt):
                libs.append(os.path.join(m.group(1).replace("\\\\", "\\"), "steamapps"))
        except Exception:
            pass
    for lib in libs:
        cand = os.path.join(lib, "common", "Slay the Spire 2")
        if os.path.isdir(cand):
            return cand
    return os.path.join(steam, "steamapps", "common", "Slay the Spire 2")


def _find_uploader():
    cands = []
    e = os.environ.get("STS2_UPLOADER_DIR") or CONFIG.get("uploader_dir")
    if e:
        cands.append(e)
    cands += [
        HERE,
        os.path.join(HERE, "ModUploader-win-x64"),
        os.path.join(os.path.expanduser("~"), "Downloads", "ModUploader-win-x64"),
    ]
    for d in cands:
        if os.path.isfile(os.path.join(d, "ModUploader.exe")):
            return d, os.path.join(d, "ModUploader.exe")
    base = e or HERE
    return base, os.path.join(base, "ModUploader.exe")


GAME_PATH = _find_game_path()
GAME_MODS = os.path.join(GAME_PATH, "mods")
UPLOADER_DIR, UPLOADER_EXE = _find_uploader()
WORKSPACES = os.environ.get("STS2_WORKSPACES") or CONFIG.get("workspaces") or os.path.join(HERE, ".workshop")

# config.json "sources": { "<ModId>": "path/to/Mod.csproj" } -> that mod gets Build+Upload
SOURCES = CONFIG.get("sources", {}) or {}

# Steam preview image limit (1 MB)
IMAGE_LIMIT = 1_048_576

# runtime junk to drop from packaged content (may sit inside installed mods/<id>/)
EXCLUDE_GLOBS = ["*.preset", "*.log", "*.tmp", "mod_id.txt",
                 "Thumbs.db", ".DS_Store", "desktop.ini"] + list(CONFIG.get("exclude", []))

PORT = int(os.environ.get("STS2_DASH_PORT", str(CONFIG.get("port", 8791))))

# only one upload at a time (uploader / Steam is single-instance)
RUN_LOCK = threading.Lock()


# ----------------------------------------------------------------------------
# 게임 버전 / 템플릿 이미지
# ----------------------------------------------------------------------------
def game_version():
    try:
        with open(os.path.join(GAME_PATH, "release_info.json"), encoding="utf-8") as f:
            return json.load(f).get("version", "?")
    except Exception:
        return "?"


GAME_VERSION = game_version()


def template_image():
    """Default placeholder for mods without an image.png. Generated on first use."""
    p = os.path.join(HERE, ".placeholder.png")
    if os.path.isfile(p):
        return p
    try:
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (640, 360), (24, 28, 36))
        d = ImageDraw.Draw(im)
        d.rectangle([8, 8, 631, 351], outline=(70, 80, 95), width=2)
        d.text((24, 24), "Set a thumbnail (image.png)", fill=(150, 160, 172))
        im.save(p, "PNG")
        return p
    except Exception:
        return None


def _placeholder_bytes():
    tpl = template_image()
    if tpl and os.path.isfile(tpl):
        with open(tpl, "rb") as f:
            return f.read()
    return None


def png_dims(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            w, h = struct.unpack(">II", data[16:24])
            return w, h
        except Exception:
            pass
    return 0, 0


# ----------------------------------------------------------------------------
# 모드 스캔
# ----------------------------------------------------------------------------
def _read_manifest(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("id") and d.get("name"):
            return d
    except Exception:
        pass
    return None


def _find_manifest(mod_dir):
    """모드 폴더 최상위에서 id/name 가진 매니페스트 json 1개를 찾는다."""
    try:
        for fn in sorted(os.listdir(mod_dir)):
            if fn.lower().endswith(".json"):
                d = _read_manifest(os.path.join(mod_dir, fn))
                if d:
                    return os.path.join(mod_dir, fn), d
    except Exception:
        pass
    return None, None


def _find_csproj(mod_dir, mod_id):
    cands = []
    try:
        for fn in os.listdir(mod_dir):
            if fn.lower().endswith(".csproj"):
                cands.append(os.path.join(mod_dir, fn))
    except Exception:
        return None
    if not cands:
        return None
    for c in cands:
        if os.path.splitext(os.path.basename(c))[0].lower() == mod_id.lower():
            return c
    return cands[0]


def workspace_dir(mod_id):
    return os.path.join(WORKSPACES, mod_id)


def read_mod_id(mod_id):
    p = os.path.join(workspace_dir(mod_id), "mod_id.txt")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                t = f.read().strip()
            if t.isdigit():
                return t
        except Exception:
            pass
    return None


def read_last_upload(mod_id):
    """마지막 업로드 시각(epoch). last_upload.txt 우선, 없으면 mod_id.txt mtime fallback."""
    p = os.path.join(workspace_dir(mod_id), "last_upload.txt")
    if os.path.isfile(p):
        try:
            return float(open(p, encoding="utf-8").read().strip())
        except Exception:
            pass
    mp = os.path.join(workspace_dir(mod_id), "mod_id.txt")
    if os.path.isfile(mp):
        try:
            return os.path.getmtime(mp)
        except Exception:
            pass
    return None


def touch_last_upload(mod_id):
    try:
        with open(os.path.join(workspace_dir(mod_id), "last_upload.txt"), "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def read_uploaded_version(mod_id):
    """마지막으로 업로드한 시점의 모드 버전(문자열). 없으면 None.
    uploaded_version.txt 우선, 없으면 마지막 패키징된 content/ 매니페스트 버전(= 실제 배포본)."""
    p = os.path.join(workspace_dir(mod_id), "uploaded_version.txt")
    if os.path.isfile(p):
        try:
            return open(p, encoding="utf-8").read().strip() or None
        except Exception:
            pass
    cdir = os.path.join(workspace_dir(mod_id), "content")
    if os.path.isdir(cdir):
        _, manifest = _find_manifest(cdir)
        if manifest and manifest.get("version"):
            return str(manifest["version"])
    return None


def write_uploaded_version(mod_id, version):
    try:
        with open(os.path.join(workspace_dir(mod_id), "uploaded_version.txt"), "w", encoding="utf-8") as f:
            f.write(str(version or ""))
    except Exception:
        pass


def write_mod_id(mod_id, value):
    """워크샵 아이템 ID 를 .workshop/<id>/mod_id.txt 에 기록(또는 빈 값이면 제거).
    기존 아이템이 있는데 로컬 mod_id 가 없을 때 수동 연결 → 업로드 시 중복 생성 방지."""
    ws = workspace_dir(mod_id)
    p = os.path.join(ws, "mod_id.txt")
    v = str(value or "").strip()
    if v == "":
        if os.path.isfile(p):
            os.remove(p)
        return None
    if not v.isdigit():
        raise RuntimeError("워크샵 아이템 ID는 숫자여야 합니다.")
    os.makedirs(ws, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(v + "\n")
    return v


def default_workshop_cfg(manifest):
    return {
        "title": manifest.get("name", manifest.get("id", "")),
        "description": manifest.get("description", ""),
        "visibility": "private",
        "changeNote": manifest.get("version", ""),
        "tags": [],
        "dependencies": [],
        "descMode": "bbcode",  # bbcode | plain (UI 토글, 업로더는 무시)
    }


def read_workshop_cfg(mod_id, manifest):
    p = os.path.join(workspace_dir(mod_id), "workshop.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
            base = default_workshop_cfg(manifest)
            base.update({k: v for k, v in cfg.items() if v is not None})
            return base
        except Exception:
            pass
    return default_workshop_cfg(manifest)


def image_status(mod_id):
    """워크스페이스 image.png 상태 (등록/placeholder/크기/해상도)."""
    st = {"exists": False, "placeholder": False, "size": 0, "w": 0, "h": 0,
          "over_limit": False}
    p = os.path.join(workspace_dir(mod_id), "image.png")
    if not os.path.isfile(p):
        return st
    with open(p, "rb") as f:
        data = f.read()
    st["exists"] = True
    st["size"] = len(data)
    st["over_limit"] = len(data) > IMAGE_LIMIT
    ph = _placeholder_bytes()
    if ph is not None and data == ph:
        st["placeholder"] = True
    st["w"], st["h"] = png_dims(data)
    return st


def content_files(mod_id):
    c = os.path.join(workspace_dir(mod_id), "content")
    if not os.path.isdir(c):
        return 0
    return sum(len(files) for _, _, files in os.walk(c))


def build_checks(cfg, installed, img, item_id):
    """등록 검증 체크리스트."""
    title_ok = bool((cfg.get("title") or "").strip())
    desc_ok = bool((cfg.get("description") or "").strip())
    image_ok = img["exists"] and not img["placeholder"] and not img["over_limit"]
    checks = {
        "title": title_ok,
        "description": desc_ok,
        "image": image_ok,
        "content": installed,
        "uploaded": item_id is not None,
    }
    # 게시 준비 = 타이틀+설명+썸네일+패키징가능
    checks["ready"] = title_ok and desc_ok and image_ok and installed
    return checks


def discover_mods():
    """Scan the game's mods/ folder (each subfolder with an id/name manifest)."""
    mods = []
    if not os.path.isdir(GAME_MODS):
        return mods
    for fn in sorted(os.listdir(GAME_MODS)):
        folder = os.path.join(GAME_MODS, fn)
        if not os.path.isdir(folder):
            continue
        manifest_path, manifest = _find_manifest(folder)
        if not manifest:
            continue
        mod_id = manifest["id"]
        cfg = read_workshop_cfg(mod_id, manifest)
        item_id = read_mod_id(mod_id)
        img = image_status(mod_id)
        version = str(manifest.get("version", "?"))
        uploaded_version = read_uploaded_version(mod_id)
        needs_update = bool(item_id and uploaded_version and uploaded_version != version)
        csproj = SOURCES.get(mod_id)
        if csproj and not os.path.isfile(csproj):
            csproj = None
        mod_dir = os.path.dirname(csproj) if csproj else folder
        mods.append(
            {
                "id": mod_id,
                "name": manifest.get("name", mod_id),
                "version": version,
                "uploaded_version": uploaded_version,
                "needs_update": needs_update,
                "has_pck": bool(manifest.get("has_pck")),
                "has_dll": bool(manifest.get("has_dll")),
                "affects_gameplay": bool(manifest.get("affects_gameplay")),
                "mod_dir": mod_dir,
                "manifest_path": manifest_path,
                "csproj": csproj,
                "installed": True,
                "installed_path": folder,
                "workshop_item_id": item_id,
                "last_upload": read_last_upload(mod_id),
                "cfg": cfg,
                "image": img,
                "content_files": content_files(mod_id),
                "checks": build_checks(cfg, True, img, item_id),
            }
        )
    mods.sort(key=lambda m: (m["last_upload"] is None, m["name"].lower()))
    return mods


def get_mod(mod_id):
    for m in discover_mods():
        if m["id"] == mod_id:
            return m
    return None


# ----------------------------------------------------------------------------
# 워크스페이스 준비 / content 동기화
# ----------------------------------------------------------------------------
def ensure_workspace(mod):
    ws = workspace_dir(mod["id"])
    os.makedirs(os.path.join(ws, "content"), exist_ok=True)
    cfgp = os.path.join(ws, "workshop.json")
    if not os.path.isfile(cfgp):
        save_workshop_cfg(mod["id"], mod["cfg"])
    imgp = os.path.join(ws, "image.png")
    if not os.path.isfile(imgp):
        tpl = template_image()
        if tpl:
            shutil.copyfile(tpl, imgp)
    return ws


def save_workshop_cfg(mod_id, cfg):
    ws = workspace_dir(mod_id)
    os.makedirs(ws, exist_ok=True)
    out = {
        "title": cfg.get("title", ""),
        "description": cfg.get("description", ""),
        "visibility": cfg.get("visibility", "private"),
        "changeNote": cfg.get("changeNote", ""),
        "tags": cfg.get("tags", []),
        "dependencies": cfg.get("dependencies", []),
        "descMode": cfg.get("descMode", "bbcode"),
    }
    with open(os.path.join(ws, "workshop.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def _write_optimized_image(mod_id, im):
    """PIL 이미지를 1MB 이하 PNG 로 최적화해 워크스페이스 image.png 로 저장."""
    import io as _io
    from PIL import Image
    ws = workspace_dir(mod_id)
    os.makedirs(ws, exist_ok=True)
    dst = os.path.join(ws, "image.png")
    w, h = im.size
    scale = 1.0
    while True:
        out = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS) if scale < 1.0 else im
        buf = _io.BytesIO()
        out.save(buf, "PNG", optimize=True)
        if buf.tell() <= IMAGE_LIMIT or scale < 0.4:
            with open(dst, "wb") as f:
                f.write(buf.getvalue())
            return buf.tell()
        scale -= 0.05


def set_image_from_path(mod_id, src_path):
    """이미지 파일 경로를 워크스페이스 image.png 로 등록 (PNG 변환 + 1MB 이하 최적화)."""
    if not os.path.isfile(src_path):
        raise RuntimeError(f"이미지 파일 없음: {src_path}")
    try:
        from PIL import Image
        return _write_optimized_image(mod_id, Image.open(src_path).convert("RGB"))
    except ImportError:
        ws = workspace_dir(mod_id)
        os.makedirs(ws, exist_ok=True)
        dst = os.path.join(ws, "image.png")
        shutil.copyfile(src_path, dst)
        return os.path.getsize(dst)


def set_image_from_bytes(mod_id, data):
    """브라우저에서 업로드된 이미지 바이트를 워크스페이스 image.png 로 등록."""
    if not data:
        raise RuntimeError("빈 이미지 데이터")
    try:
        import io as _io
        from PIL import Image
        return _write_optimized_image(mod_id, Image.open(_io.BytesIO(data)).convert("RGB"))
    except ImportError:
        ws = workspace_dir(mod_id)
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "image.png"), "wb") as f:
            f.write(data)
        return len(data)


def sync_content(mod, log):
    """게임 mods/<id>/ 의 설치 내용을 워크스페이스 content/ 로 복사."""
    src = mod["installed_path"]
    if not os.path.isdir(src):
        raise RuntimeError(
            f"설치된 모드 폴더가 없습니다: {src}\n"
            f"먼저 게임에 모드를 빌드/설치하거나 [Build+Upload] 를 사용하세요."
        )
    dst = os.path.join(workspace_dir(mod["id"]), "content")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    skipped = []

    def _ignore(dirpath, names):
        drop = set(shutil.ignore_patterns(*EXCLUDE_GLOBS)(dirpath, names))
        for nm in drop:
            if os.path.isfile(os.path.join(dirpath, nm)):
                skipped.append(os.path.relpath(os.path.join(dirpath, nm), src))
        return drop

    shutil.copytree(src, dst, ignore=_ignore)
    n = sum(len(files) for _, _, files in os.walk(dst))
    log(f"[content] {src} -> content/ ({n} files)")
    for s in skipped:
        log(f"[content] 제외(런타임 부산물): {s}")


# ----------------------------------------------------------------------------
# 서브프로세스 스트리밍
# ----------------------------------------------------------------------------
def stream_cmd(cmd, cwd, log):
    log(f"$ {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        log(f"[error] 실행파일을 찾을 수 없음: {cmd[0]}")
        return 1
    for line in proc.stdout:
        log(line.rstrip("\n"))
    proc.wait()
    log(f"[exit {proc.returncode}]")
    return proc.returncode


def run_pipeline(mod, do_build, log):
    """build(optional) -> sync content -> uploader. 성공시 workshop url 반환."""
    if not os.path.isfile(UPLOADER_EXE):
        raise RuntimeError(f"ModUploader.exe 없음: {UPLOADER_EXE}")

    ensure_workspace(mod)

    # 게시 전 검증 경고 (차단하진 않음 — 업데이트 시 일부 필드 생략 가능)
    ck = mod["checks"]
    if not ck["image"]:
        log("[warn] 썸네일 미등록 또는 placeholder/용량초과 — 미리보기 이미지가 비어보일 수 있습니다.")
    if not ck["description"]:
        log("[warn] 설명이 비어 있습니다.")
    if not ck["title"]:
        log("[warn] 타이틀이 비어 있습니다.")

    if do_build:
        if not mod["csproj"]:
            log("[build] csproj 가 없어 빌드를 건너뜁니다.")
        else:
            log("=== Build (Release) ===")
            rc = stream_cmd(["dotnet", "build", "-c", "Release", mod["csproj"]],
                            cwd=mod["mod_dir"], log=log)
            if rc != 0:
                raise RuntimeError("빌드 실패 — 업로드를 중단합니다.")

    log("=== Package content ===")
    sync_content(mod, log)

    log("=== Upload to Steam Workshop ===")
    ws = workspace_dir(mod["id"])
    rc = stream_cmd([UPLOADER_EXE, "upload", "-w", ws],
                    cwd=UPLOADER_DIR, log=log)
    if rc != 0:
        raise RuntimeError("업로더가 실패했습니다 (Steam 실행/로그인 상태 확인).")

    touch_last_upload(mod["id"])
    write_uploaded_version(mod["id"], mod["version"])
    item_id = read_mod_id(mod["id"])
    if item_id:
        url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}"
        log(f"[done] 워크샵 아이템: {url}")
        return url
    return None


def steam_running():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq steam.exe"],
            capture_output=True, text=True, timeout=5,
        ).stdout.lower()
        return "steam.exe" in out
    except Exception:
        return None


# ----------------------------------------------------------------------------
# HTML (master-detail)
# ----------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>STS2 Workshop Upload</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{font:14px/1.5 system-ui,Segoe UI,sans-serif;background:#14161a;color:#e6e6e6;margin:0;padding:16px}
  h1{font-size:17px;margin:0 0 2px}
  .sub{color:#8b95a1;font-size:12px}
  .warn{background:#3a2a12;border:1px solid #7a5a1a;color:#f0c674;padding:7px 11px;border-radius:6px;margin:10px 0 0;font-size:13px}
  .wrap{display:flex;gap:16px;margin-top:14px;align-items:flex-start}
  .list{width:330px;flex:none;border:1px solid #262a31;border-radius:8px;overflow:hidden}
  .li{padding:9px 12px;border-bottom:1px solid #20242b;cursor:pointer;display:flex;gap:8px;align-items:center}
  .li:hover{background:#1b1f26}
  .li.sel{background:#1d2734;border-left:3px solid #2563eb;padding-left:9px}
  .li .nm{flex:1;min-width:0}
  .li .nm b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .li .nm span{color:#8b95a1;font-size:11px}
  .dot{width:9px;height:9px;border-radius:50%;flex:none}
  .dot.ready{background:#6ee7a0}.dot.partial{background:#f0c674}.dot.bad{background:#5b6270}
  .badge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600}
  .ok{background:#16361f;color:#6ee7a0}.no{background:#3a1f22;color:#f08a8a}.muted{background:#23262c;color:#8b95a1}
  .upd{background:#3a2e12;color:#f0b840}
  .detail{flex:1;min-width:0;border:1px solid #262a31;border-radius:8px;padding:16px}
  .empty{color:#8b95a1;padding:40px;text-align:center}
  .dhead{display:flex;gap:14px;align-items:flex-start}
  .thumb{width:240px;flex:none}
  .thumb img{width:240px;height:auto;border:1px solid #313640;border-radius:6px;background:#0c0e11;display:block}
  .thumb .meta{color:#8b95a1;font-size:11px;margin-top:4px}
  .checks{margin:0;padding:0;list-style:none;font-size:13px}
  .checks li{padding:2px 0}
  .ci{display:inline-block;width:18px}
  .pass{color:#6ee7a0}.fail{color:#f08a8a}
  .form{margin-top:14px;display:grid;grid-template-columns:96px 1fr;gap:8px;align-items:start}
  .form label{color:#8b95a1;font-size:12px;padding-top:6px}
  input,textarea,select{width:100%;background:#1c1f25;color:#e6e6e6;border:1px solid #313640;border-radius:4px;padding:6px 8px;font:inherit}
  textarea{resize:vertical;font:12px/1.5 Consolas,monospace}
  .bbbar{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:4px;align-items:center}
  .bbbar button{padding:2px 7px;font-size:11px;background:#2b303a}
  .bbbar button.mode.active{background:#2563eb}
  #bbtools{display:inline-flex;gap:4px;flex-wrap:wrap}
  .preview{border:1px solid #313640;border-radius:4px;padding:10px 12px;min-height:140px;background:#0f1217;font-size:13px}
  .preview h1{font-size:19px;margin:.4em 0}.preview h2{font-size:16px;margin:.4em 0}.preview h3{font-size:14px;margin:.3em 0}
  .preview ul,.preview ol{margin:4px 0 4px 20px;padding:0}
  .preview hr{border:0;border-top:1px solid #313640;margin:9px 0}
  .preview a{color:#6ea8fe}
  .preview blockquote{border-left:3px solid #3a3f48;margin:6px 0;padding:2px 10px;color:#b9c2cc}
  .preview pre{background:#0a0c0f;padding:8px;border-radius:4px;overflow:auto}
  button{background:#2563eb;color:#fff;border:0;padding:7px 14px;border-radius:5px;cursor:pointer;font-size:13px}
  button.sec{background:#3a3f48}
  button:disabled{opacity:.45;cursor:default}
  .actions{margin-top:14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .topbar{display:flex;justify-content:space-between;align-items:center;gap:10px}
  .topbar select{width:auto}
  a{color:#6ea8fe}
  #log{background:#0c0e11;border:1px solid #262a31;border-radius:6px;padding:10px;height:230px;overflow:auto;
       white-space:pre-wrap;font:12px/1.45 Consolas,monospace;margin-top:14px;color:#cbd3dc}
</style></head><body>
<div class="topbar">
  <h1 id="h_title"></h1>
  <select id="lang" onchange="setLang(this.value)">
    <option value="en">English</option>
    <option value="ko">한국어</option>
  </select>
</div>
<div class="sub"><span id="l_game"></span>: <b id="gv"></b> · <span id="l_up"></span>: <span id="upx"></span> · <span id="l_hint"></span></div>
<div class="sub" style="margin-top:6px"><span id="l_sort"></span>:
  <select id="sort" onchange="SORT=this.value;renderList()">
    <option value="recent"></option><option value="name"></option><option value="installed"></option>
  </select>
</div>
<div id="warn"></div>

<div class="wrap">
  <div class="list" id="list"></div>
  <div class="detail" id="detail"></div>
</div>

<script>
const I18N={
 en:{title:"STS2 Steam Workshop Upload",gameInstall:"Game build",uploader:"Uploader",
  hint:"select a mod on the left to review and upload",sortLabel:"Sort",
  sortRecent:"Recently uploaded",sortName:"Name",sortInstalled:"Installed first",
  warnNoUploader:"ModUploader.exe not found: ",warnSteam:"Steam does not appear to be running. Start Steam and log in before uploading.",
  emptyDetail:"Select a mod on the left.",badgeInstalled:"installed",badgeNotInstalled:"not installed",unpublished:"unpublished",
  never:"never",now:"just now",min:"m ago",hour:"h ago",day:"d ago",
  imgNone:"none (placeholder will be used)",imgPlaceholder:"⚠ placeholder (replace it)",imgOver:" · ⚠ over 1MB",
  packagable:"{n} files ready to package",notInstalled:"not installed — needs Build+Upload",workshop:"Workshop",
  chkTitle:"Title set",chkDesc:"Description set",chkImage:"Thumbnail set (not placeholder/over 1MB)",
  chkContent:"Content to upload (installed)",chkItem:"Workshop item exists",chkItemNew:"(will be created)",
  readyYes:"✔ Ready to publish",readyNo:"⚠ Some items missing",
  desc:"description",edit:"Edit",preview:"Preview",uploadAs:"Upload as",bbcode:"BBCode",plain:"Plain text",
  tagsPh:"tool, ...",thumbHint:"pick a file → auto-converted to PNG under 1MB",setBtn:"Set",
  wsIdPh:"existing Workshop item ID (leave empty to create new on Upload)",apply:"Apply",
  wsIdLinked:"linked locally → Upload updates this item",
  wsIdMissing:"no local mod_id — paste an existing ID to avoid a duplicate",
  saveCfg:"Save config",privateNote:"※ private — switch to public and Upload again when ready",
  logReady:"Ready. Steam must be running and logged in to upload.",
  busy:"[busy] another upload is in progress",cfgSaved:"[config] saved",
  imgPick:"[img] pick a file first",imgSet:"[img] set",imgFail:"[img] failed: ",
  modidLinked:"[modid] linked: ",modidRemoved:"[modid] removed (a new item will be created)",modidFail:"[modid] failed: ",
  doneOk:"✅ done",doneFail:"❌ failed: ",closed:"[connection closed]",
  needsUpdate:"Update needed",deployed:"deployed",current:"current"},
 ko:{title:"STS2 Steam Workshop 업로드",gameInstall:"게임 설치본",uploader:"업로더",
  hint:"목록의 모드를 선택하면 우측에서 검토·업로드",sortLabel:"정렬",
  sortRecent:"최신 업로드순",sortName:"이름순",sortInstalled:"설치 먼저",
  warnNoUploader:"ModUploader.exe 를 찾을 수 없습니다: ",warnSteam:"Steam 이 실행되지 않은 것 같습니다. 업로드 전에 Steam 을 켜고 로그인하세요.",
  emptyDetail:"왼쪽에서 모드를 선택하세요.",badgeInstalled:"설치됨",badgeNotInstalled:"미설치",unpublished:"미게시",
  never:"미업로드",now:"방금",min:"분 전",hour:"시간 전",day:"일 전",
  imgNone:"미등록 (placeholder 사용 예정)",imgPlaceholder:"⚠ placeholder (교체 필요)",imgOver:" · ⚠ 1MB 초과",
  packagable:"설치본 {n}개 패키징 가능",notInstalled:"미설치 — Build+Upload 필요",workshop:"워크샵",
  chkTitle:"타이틀 등록",chkDesc:"설명(BBCode) 등록",chkImage:"썸네일 등록 (placeholder/1MB초과 아님)",
  chkContent:"업로드할 content (설치본)",chkItem:"워크샵 아이템 존재",chkItemNew:"(신규 생성됨)",
  readyYes:"✔ 게시 준비 완료",readyNo:"⚠ 미충족 항목 있음",
  desc:"설명",edit:"편집",preview:"미리보기",uploadAs:"업로드 형식",bbcode:"BBCode",plain:"일반텍스트",
  tagsPh:"tool, ...",thumbHint:"파일 선택 → 1MB 이하 PNG 자동 변환",setBtn:"등록",
  wsIdPh:"기존 워크샵 아이템 ID (없으면 비워두면 Upload 시 새로 생성)",apply:"적용",
  wsIdLinked:"로컬 연결됨 → Upload 시 이 아이템 갱신",
  wsIdMissing:"로컬 mod_id 없음 — 기존 아이템이 있으면 ID 입력(중복 생성 방지)",
  saveCfg:"config 저장",privateNote:"※ private — 확인 후 public 으로 바꿔 다시 Upload",
  logReady:"준비됨. 업로드 시 Steam 이 실행·로그인된 상태여야 합니다.",
  busy:"[busy] 다른 업로드가 진행 중",cfgSaved:"[config] 저장됨",
  imgPick:"[img] 파일을 선택하세요",imgSet:"[img] 등록됨",imgFail:"[img] 실패: ",
  modidLinked:"[modid] 연결: ",modidRemoved:"[modid] 제거됨(새 아이템 생성 예정)",modidFail:"[modid] 실패: ",
  doneOk:"✅ 완료",doneFail:"❌ 실패: ",closed:"[연결 종료]",
  needsUpdate:"업데이트 필요",deployed:"배포",current:"현재"}
};
let MODS=[], SEL=null, busy=false, SORT='recent';
let LANG=localStorage.getItem('lang')||((navigator.language||'').startsWith('ko')?'ko':'en');
const $=s=>document.querySelector(s);
function t(k){return (I18N[LANG]&&I18N[LANG][k])||I18N.en[k]||k;}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function kb(n){return n>=1048576?(n/1048576).toFixed(2)+' MB':(n/1024).toFixed(0)+' KB';}
function relTime(x){
  if(!x) return t('never');
  const s=Date.now()/1000-x;
  if(s<60) return t('now');
  if(s<3600) return Math.floor(s/60)+t('min');
  if(s<86400) return Math.floor(s/3600)+t('hour');
  return Math.floor(s/86400)+t('day');
}
function sortMods(){
  const a=[...MODS], byName=(x,y)=>x.name.toLowerCase()<y.name.toLowerCase()?-1:1;
  if(SORT==='name') a.sort(byName);
  else if(SORT==='installed') a.sort((x,y)=>x.installed===y.installed?byName(x,y):(x.installed?-1:1));
  else a.sort((x,y)=>{const lx=x.last_upload||-1,ly=y.last_upload||-1;return lx!==ly?ly-lx:byName(x,y);});
  return a;
}
function setLang(l){LANG=l;localStorage.setItem('lang',l);applyLang();}
function applyLang(){
  $('#h_title').textContent=t('title');
  $('#l_game').textContent=t('gameInstall');
  $('#l_up').textContent=t('uploader');
  $('#l_hint').textContent=t('hint');
  $('#l_sort').textContent=t('sortLabel');
  const so=$('#sort').options; so[0].textContent=t('sortRecent'); so[1].textContent=t('sortName'); so[2].textContent=t('sortInstalled');
  $('#lang').value=LANG;
  renderWarn();
  renderList();
  const m=SEL&&MODS.find(x=>x.id===SEL);
  if(m) renderDetail(m); else $('#detail').innerHTML='<div class="empty">'+esc(t('emptyDetail'))+'</div>';
}

let STATE={};
async function load(keep){
  const r=await fetch('/api/state'); STATE=await r.json();
  MODS=STATE.mods; $('#gv').textContent=STATE.game_version; $('#upx').textContent=STATE.uploader_ok?'OK':'—';
  applyLang();
}
function renderWarn(){
  const w=$('#warn'); if(!w) return; w.innerHTML='';
  if(!STATE.uploader_ok) w.innerHTML+='<div class="warn">'+esc(t('warnNoUploader'))+esc(STATE.uploader_exe||'')+'</div>';
  if(STATE.steam_running===false) w.innerHTML+='<div class="warn">'+esc(t('warnSteam'))+'</div>';
}
function dotClass(m){ if(m.checks.ready) return 'ready'; if(m.checks.title&&m.checks.description) return 'partial'; return 'bad'; }
function renderList(){
  const L=$('#list'); L.innerHTML='';
  for(const m of sortMods()){
    const div=document.createElement('div');
    div.className='li'+(m.id===SEL?' sel':'');
    div.innerHTML=`<span class="dot ${dotClass(m)}" title="ready=${m.checks.ready}"></span>
      <span class="nm"><b>${esc(m.name)}</b><span>${esc(m.version)} · ${m.workshop_item_id?esc(relTime(m.last_upload)):esc(t('unpublished'))}</span></span>
      ${m.needs_update?'<span class="badge upd" title="'+esc(t('needsUpdate'))+'">⟳</span>':''}
      ${m.installed?'<span class="badge ok">'+esc(t('badgeInstalled'))+'</span>':'<span class="badge no">'+esc(t('badgeNotInstalled'))+'</span>'}
      ${m.workshop_item_id?'<span class="badge muted">●</span>':''}`;
    div.onclick=()=>{SEL=m.id;renderList();renderDetail(m);};
    L.appendChild(div);
  }
}
function ck(b){return b?'<span class="ci pass">✔</span>':'<span class="ci fail">✘</span>';}
function renderDetail(m){
  const im=m.image;
  const noteVal=(m.needs_update&&(!m.cfg.changeNote||m.cfg.changeNote===m.uploaded_version))?m.version:m.cfg.changeNote;
  const imgNote = !im.exists ? t('imgNone')
      : im.placeholder ? t('imgPlaceholder')
      : (im.w+'×'+im.h+' · '+kb(im.size)+(im.over_limit?t('imgOver'):''));
  const D=$('#detail');
  D.innerHTML=`
    <div class="dhead">
      <div class="thumb">
        <img src="/api/image?id=${encodeURIComponent(m.id)}&t=${im.size}" alt="thumb">
        <div class="meta">${esc(imgNote)}</div>
      </div>
      <div style="flex:1;min-width:0">
        <h1 style="font-size:16px">${esc(m.name)} <span class="sub">${esc(m.version)}</span></h1>
        <div class="sub" style="margin-bottom:8px">${esc(m.id)} ·
          ${m.installed?esc(t('packagable').replace('{n}',m.content_files)):'<span class="fail">'+esc(t('notInstalled'))+'</span>'} ·
          ${m.workshop_item_id?(esc(t('workshop'))+' <a target="_blank" href="https://steamcommunity.com/sharedfiles/filedetails/?id='+m.workshop_item_id+'">'+m.workshop_item_id+'</a>'):'<span class="muted">'+esc(t('unpublished'))+'</span>'}
        </div>
        ${m.needs_update?'<div class="sub" style="color:#f0b840;margin-bottom:8px;font-weight:600">⟳ '+esc(t('needsUpdate'))+': '+esc(t('deployed'))+' '+esc(m.uploaded_version)+' → '+esc(t('current'))+' '+esc(m.version)+'</div>':''}
        <ul class="checks">
          <li>${ck(m.checks.title)} ${esc(t('chkTitle'))}</li>
          <li>${ck(m.checks.description)} ${esc(t('chkDesc'))}</li>
          <li>${ck(m.checks.image)} ${esc(t('chkImage'))}</li>
          <li>${ck(m.checks.content)} ${esc(t('chkContent'))}</li>
          <li>${ck(m.checks.uploaded)} ${esc(t('chkItem'))} ${m.workshop_item_id?'':esc(t('chkItemNew'))}</li>
        </ul>
        <div style="margin-top:6px;font-weight:600">${m.checks.ready?'<span class="pass">'+esc(t('readyYes'))+'</span>':'<span class="fail">'+esc(t('readyNo'))+'</span>'}</div>
      </div>
    </div>

    <div class="form">
      <label>title</label><input id="f_title" value="${esc(m.cfg.title)}">
      <label>${esc(t('desc'))}</label>
      <div>
        <div class="bbbar">
          <button class="sec mode" id="v_edit" onclick="setView('edit')">${esc(t('edit'))}</button>
          <button class="sec mode" id="v_prev" onclick="setView('preview')">${esc(t('preview'))}</button>
          <span id="bbtools" style="margin-left:8px">
            <button class="sec" data-bb="[b]|[/b]">b</button>
            <button class="sec" data-bb="[i]|[/i]">i</button>
            <button class="sec" data-bb="[h2]|[/h2]">h2</button>
            <button class="sec" data-bb="[list]\n[*]|\n[/list]">list</button>
            <button class="sec" data-bb="[url=]|[/url]">url</button>
            <button class="sec" data-bb="[hr][/hr]|">hr</button>
          </span>
          <span style="flex:1"></span>
          <span class="sub" style="margin-right:4px">${esc(t('uploadAs'))}</span>
          <button class="sec mode" id="m_bb" onclick="setMode('bbcode')">${esc(t('bbcode'))}</button>
          <button class="sec mode" id="m_pl" onclick="setMode('plain')">${esc(t('plain'))}</button>
        </div>
        <textarea id="f_desc" rows="12" oninput="if(window.DVIEW==='preview')applyDescUI()">${esc(m.cfg.description)}</textarea>
        <div id="f_preview" class="preview" style="display:none"></div>
      </div>
      <label>tags</label><input id="f_tags" value="${esc((m.cfg.tags||[]).join(', '))}" placeholder="${esc(t('tagsPh'))}">
      <label>changeNote</label><input id="f_note" value="${esc(noteVal)}">
      <label>visibility</label>
      <select id="f_vis">
        ${['private','public','unlisted','friends_only'].map(v=>`<option value="${v}"${m.cfg.visibility===v?' selected':''}>${v}</option>`).join('')}
      </select>
      <label>thumbnail</label>
      <div style="display:flex;gap:6px;align-items:center">
        <input type="file" id="f_imgfile" accept="image/png,image/jpeg,image/webp">
        <button class="sec" style="flex:none" onclick="uploadImg('${m.id}')">${esc(t('setBtn'))}</button>
        <span class="sub">${esc(t('thumbHint'))}</span>
      </div>
      <label>workshop ID</label>
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        <input id="f_modid" inputmode="numeric" value="${m.workshop_item_id||''}"
          placeholder="${esc(t('wsIdPh'))}" style="max-width:280px">
        <button class="sec" style="flex:none" onclick="saveModId('${m.id}')">${esc(t('apply'))}</button>
        ${m.workshop_item_id
          ? '<span class="sub">'+esc(t('wsIdLinked'))+'</span>'
          : '<span class="sub fail">'+esc(t('wsIdMissing'))+'</span>'}
      </div>
    </div>

    <div class="actions">
      <button class="sec" onclick="save('${m.id}')">${esc(t('saveCfg'))}</button>
      <button onclick="run('${m.id}',false)" ${m.installed?'':'disabled'}>Upload</button>
      <button class="sec" onclick="run('${m.id}',true)" ${m.csproj?'':'disabled'}>Build+Upload</button>
      ${m.cfg.visibility==='private'?'<span class="sub">'+esc(t('privateNote'))+'</span>':''}
    </div>
    <div id="log">${esc(t('logReady'))}\n</div>`;

  D.querySelectorAll('[data-bb]').forEach(b=>b.onclick=e=>{e.preventDefault();bb($('#f_desc'),b.dataset.bb);});
  window.DVIEW='edit'; window.DMODE=m.cfg.descMode||'bbcode'; applyDescUI();
}
function renderBBCode(src){
  let s=esc(src);
  s=s.replace(/\[hr\](\[\/hr\])?/g,'<hr>');
  s=s.replace(/\[h1\]([\s\S]*?)\[\/h1\]/g,'<h1>$1</h1>')
     .replace(/\[h2\]([\s\S]*?)\[\/h2\]/g,'<h2>$1</h2>')
     .replace(/\[h3\]([\s\S]*?)\[\/h3\]/g,'<h3>$1</h3>');
  s=s.replace(/\[b\]([\s\S]*?)\[\/b\]/g,'<b>$1</b>')
     .replace(/\[i\]([\s\S]*?)\[\/i\]/g,'<i>$1</i>')
     .replace(/\[u\]([\s\S]*?)\[\/u\]/g,'<u>$1</u>')
     .replace(/\[strike\]([\s\S]*?)\[\/strike\]/g,'<s>$1</s>');
  s=s.replace(/\[url=([^\]]+)\]([\s\S]*?)\[\/url\]/g,'<a href="$1" target="_blank">$2</a>')
     .replace(/\[url\]([\s\S]*?)\[\/url\]/g,'<a href="$1" target="_blank">$1</a>');
  s=s.replace(/\[img\]([\s\S]*?)\[\/img\]/g,'<img src="$1" style="max-width:100%">');
  s=s.replace(/\[quote\]([\s\S]*?)\[\/quote\]/g,'<blockquote>$1</blockquote>')
     .replace(/\[code\]([\s\S]*?)\[\/code\]/g,'<pre>$1</pre>');
  s=s.replace(/\[(o?)list\]([\s\S]*?)\[\/\1list\]/g,(mm,o,inner)=>{
      const items=inner.split('[*]').map(x=>x.trim()).filter(Boolean);
      const tg=o?'ol':'ul';
      return '<'+tg+'>'+items.map(x=>'<li>'+x+'</li>').join('')+'</'+tg+'>';
  });
  s=s.replace(/\n/g,'<br>');
  s=s.replace(/(<\/(h1|h2|h3|ul|ol|li|blockquote|pre)>|<hr>)<br>/g,'$1')
     .replace(/<br>(<(ul|ol|h1|h2|h3|blockquote|pre|hr))/g,'$1');
  return s;
}
function applyDescUI(){
  const edit=window.DVIEW!=='preview', plain=window.DMODE==='plain';
  const ta=$('#f_desc'), pv=$('#f_preview'), tools=$('#bbtools');
  if(ta) ta.style.display=edit?'':'none';
  if(pv){
    pv.style.display=edit?'none':'';
    if(!edit) pv.innerHTML = plain ? esc(ta.value).replace(/\n/g,'<br>') : renderBBCode(ta.value);
  }
  if(tools) tools.style.display=(edit && !plain)?'':'none';
  const set=(id,on)=>{const e=$(id);if(e)e.classList.toggle('active',on);};
  set('#v_edit',edit); set('#v_prev',!edit);
  set('#m_bb',!plain); set('#m_pl',plain);
}
function setView(x){window.DVIEW=x;applyDescUI();}
function setMode(x){window.DMODE=x;applyDescUI();}
async function uploadImg(id){
  const f=$('#f_imgfile').files[0]; if(!f){logln(t('imgPick'));return;}
  const r=await fetch('/api/image_upload?id='+encodeURIComponent(id),
    {method:'POST',headers:{'Content-Type':f.type||'application/octet-stream'},body:f});
  const d=await r.json();
  logln(d.ok?(t('imgSet')+' ('+kb(d.size)+')'):(t('imgFail')+d.error));
  load(true);
}
async function saveModId(id){
  const v=$('#f_modid').value.trim();
  const r=await fetch('/api/modid',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,mod_id:v})});
  const d=await r.json();
  logln(d.ok?(d.mod_id?(t('modidLinked')+d.mod_id):t('modidRemoved')):(t('modidFail')+d.error));
  load(true);
}
function bb(ta,tpl){
  const [a,b]=tpl.replace(/\\n/g,'\n').split('|');
  const s=ta.selectionStart,e=ta.selectionEnd,v=ta.value;
  ta.value=v.slice(0,s)+a+v.slice(s,e)+(b||'')+v.slice(e);
  ta.focus(); ta.selectionStart=ta.selectionEnd=s+a.length+(e-s);
}
function logln(x){const l=$('#log'); if(l){l.textContent+=x+"\n";l.scrollTop=l.scrollHeight;}}
function collect(id){
  const m=MODS.find(x=>x.id===id);
  m.cfg.title=$('#f_title').value;
  m.cfg.description=$('#f_desc').value;
  m.cfg.tags=$('#f_tags').value.split(',').map(s=>s.trim()).filter(Boolean);
  m.cfg.changeNote=$('#f_note').value;
  m.cfg.visibility=$('#f_vis').value;
  m.cfg.descMode=window.DMODE||'bbcode';
  return m.cfg;
}
async function save(id){
  const c=collect(id);
  await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,cfg:c})});
  logln(t('cfgSaved')); load(true);
}
function run(id,build){
  if(busy){logln(t('busy'));return;}
  busy=true; collect(id);
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,cfg:MODS.find(m=>m.id===id).cfg})})
   .then(()=>{
     logln('\n===== '+(build?'BUILD+UPLOAD':'UPLOAD')+': '+id+' =====');
     const ev=new EventSource('/api/run?id='+encodeURIComponent(id)+'&build='+(build?1:0));
     ev.onmessage=e=>{
       const d=JSON.parse(e.data);
       if(d.type==='log') logln(d.line);
       else if(d.type==='done'){ ev.close(); busy=false;
         logln(d.ok?(t('doneOk')+(d.url?': '+d.url:'')):(t('doneFail')+(d.error||'')));
         load(true);
       }
     };
     ev.onerror=()=>{ev.close();busy=false;logln(t('closed'));document.querySelectorAll('button').forEach(b=>b.disabled=false);load(true);};
   });
}
load();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        elif path == "/api/state":
            self._json({
                "game_version": GAME_VERSION,
                "uploader_exe": UPLOADER_EXE,
                "uploader_ok": os.path.isfile(UPLOADER_EXE),
                "steam_running": steam_running(),
                "mods": discover_mods(),
            })
        elif path == "/api/image":
            self._serve_image(urllib.parse.parse_qs(parsed.query))
        elif path == "/api/run":
            self._sse_run(urllib.parse.parse_qs(parsed.query))
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))

        # 파일 업로드: 본문이 raw 이미지 바이트 (JSON 아님)
        if parsed.path == "/api/image_upload":
            q = urllib.parse.parse_qs(parsed.query)
            mod_id = (q.get("id") or [""])[0]
            data = self.rfile.read(n) if n else b""
            try:
                size = set_image_from_bytes(mod_id, data)
                self._json({"ok": True, "size": size})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
            return

        body = self.rfile.read(n) if n else b""
        try:
            data = json.loads(body or b"{}")
        except Exception:
            data = {}

        if parsed.path == "/api/save":
            mod_id = data.get("id")
            if mod_id:
                save_workshop_cfg(mod_id, data.get("cfg") or {})
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "no id"}, 400)
        elif parsed.path == "/api/image_set":
            mod_id = data.get("id")
            try:
                size = set_image_from_path(mod_id, data.get("path", ""))
                self._json({"ok": True, "size": size})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif parsed.path == "/api/modid":
            mod_id = data.get("id")
            try:
                val = write_mod_id(mod_id, data.get("mod_id", ""))
                self._json({"ok": True, "mod_id": val})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        else:
            self._send(404, "text/plain", b"not found")

    def _serve_image(self, q):
        mod_id = (q.get("id") or [""])[0]
        p = os.path.join(workspace_dir(mod_id), "image.png")
        if not os.path.isfile(p):
            p = template_image()
        if not p or not os.path.isfile(p):
            self._send(404, "text/plain", b"no image")
            return
        with open(p, "rb") as f:
            data = f.read()
        self._send(200, "image/png", data, extra={"Cache-Control": "no-cache"})

    def _sse_run(self, q):
        mod_id = (q.get("id") or [""])[0]
        do_build = (q.get("build") or ["0"])[0] == "1"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def emit(obj):
            payload = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
            self.wfile.write(payload.encode("utf-8", errors="replace"))
            self.wfile.flush()

        def log(line):
            for ln in str(line).splitlines() or [""]:
                emit({"type": "log", "line": ln})

        if not RUN_LOCK.acquire(blocking=False):
            try:
                emit({"type": "done", "ok": False, "error": "다른 업로드가 진행 중"})
            except Exception:
                pass
            return
        try:
            mod = get_mod(mod_id)
            if not mod:
                emit({"type": "done", "ok": False, "error": f"모드 없음: {mod_id}"})
                return
            try:
                url = run_pipeline(mod, do_build, log)
                emit({"type": "done", "ok": True, "url": url})
            except Exception as e:
                log(f"[error] {e}")
                emit({"type": "done", "ok": False, "error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            RUN_LOCK.release()


def _bind(start_port):
    last = None
    for p in range(start_port, start_port + 20):
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), Handler), p
        except OSError as e:
            last = e
            continue
    raise last


def main():
    os.makedirs(WORKSPACES, exist_ok=True)
    srv, port = _bind(PORT)
    url = f"http://127.0.0.1:{port}"
    print("=" * 60)
    print(" STS2 Workshop 업로드 대시보드")
    print(f"   게임 설치본 : {GAME_VERSION}  ({GAME_PATH})")
    print(f"   업로더      : {'OK' if os.path.isfile(UPLOADER_EXE) else '없음 ⚠'}  ({UPLOADER_EXE})")
    print(f"   워크스페이스: {WORKSPACES}")
    print(f"\n   브라우저에서 열기 ->  {url}\n")
    print("   (Ctrl+C 로 종료)")
    print("=" * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료.")


if __name__ == "__main__":
    main()
