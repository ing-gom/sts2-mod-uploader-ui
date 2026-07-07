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

# Per-mod extra excludes. The STS2 Mod Translator writes a runtime "Translations"
# folder (user-local translation data / source dumps) into its own install dir;
# drop it for that mod only. NOTE: Windows fnmatch is case-insensitive, so a global
# "Translations" rule would also eat a translation-pack mod's lowercase "translations"
# payload folder -- hence this must stay scoped to the translator mod id.
EXCLUDE_GLOBS_BY_MOD = {"Sts2ModTranslator": ["Translations"]}

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


CREDIT_URL = "https://github.com/ing-gom/sts2-mod-uploader-ui"
_CREDIT_BB = "\n\n[hr][/hr]\n[i]Published with [url=" + CREDIT_URL + "]STS2 Mod Uploader UI[/url].[/i]"
_CREDIT_TXT = "\n\nPublished with STS2 Mod Uploader UI — " + CREDIT_URL


def _strip_credit(desc):
    return (desc or "").replace(_CREDIT_BB, "").replace(_CREDIT_TXT, "").rstrip()


def _apply_credit(desc, mode, on):
    """저장 시점에만 푸터를 붙인다(편집창은 항상 깨끗). 기존 푸터는 먼저 제거 → 중복 방지."""
    desc = _strip_credit(desc)
    if on:
        desc += _CREDIT_TXT if mode == "plain" else _CREDIT_BB
    return desc


# ----------------------------------------------------------------------------
# 언어별 설명 파일 (descriptions/<lang>.bbcode)
#   workshop.json 은 타이틀·메타데이터만 담고, 설명 본문은 언어별 파일로 분리 저장한다.
#   파일 내용 = 푸터 없는 편집 원문. 푸터는 업로드 조립 시점에만 적용.
# ----------------------------------------------------------------------------
DESC_SUBDIR = "descriptions"


def desc_dir(mod_id):
    return os.path.join(workspace_dir(mod_id), DESC_SUBDIR)


def _desc_file(mod_id, lang):
    return os.path.join(desc_dir(mod_id), ((lang or "english").strip() or "english") + ".bbcode")


def read_desc_file(mod_id, lang):
    """언어별 설명 파일 내용(푸터 없는 편집 원문). 파일이 없으면 None."""
    p = _desc_file(mod_id, lang)
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    return None


def write_desc_file(mod_id, lang, text):
    """언어별 설명을 파일로 저장(푸터 제거한 편집 원문). 빈 값이면 파일 제거."""
    p = _desc_file(mod_id, lang)
    text = _strip_credit(text or "")
    if text.strip():
        os.makedirs(desc_dir(mod_id), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    elif os.path.isfile(p):
        try:
            os.remove(p)
        except Exception:
            pass


def _prune_desc_files(mod_id, keep_langs):
    """keep_langs 에 없는 언어의 설명 파일 정리(언어 제거 시)."""
    d = desc_dir(mod_id)
    if not os.path.isdir(d):
        return
    for fn in os.listdir(d):
        if fn.endswith(".bbcode") and fn[:-len(".bbcode")] not in keep_langs:
            try:
                os.remove(os.path.join(d, fn))
            except Exception:
                pass


def migrate_split_descriptions(mod_id):
    """구형식(설명이 workshop.json 인라인)을 언어별 파일로 1회 분리. 이미 분리됐으면 no-op."""
    p = os.path.join(workspace_dir(mod_id), "workshop.json")
    if not os.path.isfile(p):
        return
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return
    prim = (cfg.get("language") or "english").strip() or "english"
    changed = False
    if "description" in cfg:
        write_desc_file(mod_id, prim, cfg.pop("description") or "")
        changed = True
    for L in (cfg.get("localizations") or []):
        if "description" in L:
            write_desc_file(mod_id, L.get("language", ""), L.pop("description") or "")
            changed = True
    if changed:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)


def assemble_workshop_json_for_upload(mod_id):
    """업로드 직전: 언어별 설명 파일을 workshop.json 에 합쳐(푸터 적용) 업로더가 읽게 한다.
    파일이 없으면 남아있는 인라인 설명(미마이그레이션)으로 폴백."""
    p = os.path.join(workspace_dir(mod_id), "workshop.json")
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    prim = (cfg.get("language") or "english").strip() or "english"
    mode = cfg.get("descMode", "bbcode")
    on = bool(cfg.get("creditFooter", False))
    body = read_desc_file(mod_id, prim)
    if body is None:
        body = _strip_credit(cfg.get("description", ""))
    cfg["description"] = _apply_credit(body, mode, on)
    for L in (cfg.get("localizations") or []):
        b = read_desc_file(mod_id, L.get("language", ""))
        if b is None:
            b = _strip_credit(L.get("description", ""))
        L["description"] = _apply_credit(b, mode, on)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def strip_desc_from_workshop_json(mod_id):
    """업로드 후: workshop.json 에서 설명 본문 제거(타이틀·메타·dependenciesLive 보존)."""
    p = os.path.join(workspace_dir(mod_id), "workshop.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return
    cfg.pop("description", None)
    for L in (cfg.get("localizations") or []):
        L.pop("description", None)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def default_workshop_cfg(manifest):
    return {
        "title": manifest.get("name", manifest.get("id", "")),
        "description": manifest.get("description", ""),
        "visibility": "private",
        "changeNote": manifest.get("version", ""),
        "tags": [],
        "dependencies": [],       # 사용자 명시 override (있으면 정확히 동기화)
        "dependenciesLive": [],   # 업로드 시 창작마당에서 자동 가져온 현재 목록 (표시용)
        "language": "english",    # 위 title/description 의 기본 언어 (Steam API 코드)
        "localizations": [],      # 추가 언어별 {language,title,description}
        "descMode": "bbcode",   # bbcode | plain (UI 토글, 업로더는 무시)
        "creditFooter": False,  # 설명 끝에 도구 표기 푸터 추가 여부 (옵션)
    }


def read_workshop_cfg(mod_id, manifest):
    p = os.path.join(workspace_dir(mod_id), "workshop.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
            base = default_workshop_cfg(manifest)
            base.update({k: v for k, v in cfg.items() if v is not None})
            base["creditFooter"] = bool(base.get("creditFooter", False))
            # 설명: 언어별 파일 우선, 없으면 workshop.json 인라인(구형식) 폴백. 편집용은 항상 푸터 제거.
            prim = (base.get("language") or "english").strip() or "english"
            base["description"] = _load_desc_for_edit(mod_id, prim, base.get("description", ""))
            for L in (base.get("localizations") or []):
                L["description"] = _load_desc_for_edit(mod_id, L.get("language", ""), L.get("description", ""))
            return base
        except Exception:
            pass
    return default_workshop_cfg(manifest)


def _load_desc_for_edit(mod_id, lang, inline_fallback):
    """편집용 설명(푸터 없음): 언어별 파일 우선, 없으면 인라인 폴백을 푸터 제거해 반환."""
    body = read_desc_file(mod_id, lang)
    if body is not None:
        return body
    return _strip_credit(inline_fallback or "")


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
    prim = (cfg.get("language") or "english").strip() or "english"
    # workshop.json = 타이틀 + 메타데이터만. 설명 본문은 언어별 파일(descriptions/<lang>.bbcode)로 분리.
    out = {
        "title": cfg.get("title", ""),
        "visibility": cfg.get("visibility", "private"),
        "changeNote": cfg.get("changeNote", ""),
        "tags": cfg.get("tags", []),
        "descMode": cfg.get("descMode", "bbcode"),
        "creditFooter": bool(cfg.get("creditFooter", False)),
        "language": prim,
    }
    # 필요한 아이템(Required Items) = Steam dependencies.
    #  비어 있으면 키 자체를 생략한다 → 패치된 업로더가 기존 required items 를 건드리지 않음
    #  (빈 배열 [] 을 쓰면 업로더가 Steam 의 기존 항목을 전부 제거해 버린다).
    #  값이 있을 때만 기록 → 그 경우엔 대시보드가 source of truth 로서 동기화.
    deps = [int(d) for d in (cfg.get("dependencies") or []) if str(d).strip().isdigit()]
    if deps:
        out["dependencies"] = deps
    # dependenciesLive = 업로더가 업로드 시 창작마당에서 자동으로 가져와 기록하는 display-only 미러.
    #  대시보드 저장이 이 값을 지우지 않도록 그대로 보존(reconcile 에는 쓰이지 않음, 표시용).
    live = [int(d) for d in (cfg.get("dependenciesLive") or []) if str(d).strip().isdigit()]
    if live:
        out["dependenciesLive"] = live
    # 설명 본문을 언어별 파일로 저장(푸터 없는 편집 원문). 기본 언어부터.
    keep_langs = {prim}
    write_desc_file(mod_id, prim, cfg.get("description", ""))
    # 다국어: 언어별 타이틀은 workshop.json 에, 설명은 파일에.
    locs = []
    for L in (cfg.get("localizations") or []):
        lang = (L.get("language") or "").strip()
        if not lang:
            continue
        keep_langs.add(lang)
        write_desc_file(mod_id, lang, L.get("description", ""))
        locs.append({"language": lang, "title": L.get("title", "")})
    if locs:
        out["localizations"] = locs
    _prune_desc_files(mod_id, keep_langs)  # 제거된 언어의 설명 파일 정리
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

    globs = EXCLUDE_GLOBS + EXCLUDE_GLOBS_BY_MOD.get(mod["id"], [])

    def _ignore(dirpath, names):
        drop = set(shutil.ignore_patterns(*globs)(dirpath, names))
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
    # 업로더는 workshop.json 의 description/localizations[].description 를 읽는다.
    #  분리 저장 구조라, 업로드 직전 언어별 파일을 합쳐 넣고(푸터 적용) 끝나면 다시 타이틀-only 로 복원.
    migrate_split_descriptions(mod["id"])       # 구형식이면 먼저 분리
    assemble_workshop_json_for_upload(mod["id"])
    rc = 1
    try:
        rc = stream_cmd([UPLOADER_EXE, "upload", "-w", ws],
                        cwd=UPLOADER_DIR, log=log)
    finally:
        strip_desc_from_workshop_json(mod["id"])  # 정본은 타이틀-only 유지(업로더가 쓴 dependenciesLive 는 보존)
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
  .listcol{width:340px;flex:none;border:1px solid #262a31;border-radius:8px;overflow:hidden;display:flex;flex-direction:column}
  .lbar{padding:8px;border-bottom:1px solid #1d222a;background:#0f1115;display:flex;flex-direction:column;gap:6px}
  .lbar input,.lbar select{background:#1c1f25;color:#e6e6e6;border:1px solid #313640;border-radius:5px;padding:5px 7px;font:inherit;font-size:12px;width:100%}
  .lbar .row2{display:flex;gap:6px}
  .lcount{color:#8b95a1;font-size:11px;padding-left:1px}
  .list{flex:1;overflow-y:auto;max-height:64vh}
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
  .tagbtns{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
  .tagbtn{padding:3px 11px;font-size:12px;background:#23262c;color:#8b95a1;border:1px solid #313640;border-radius:14px;cursor:pointer}
  .tagbtn:hover{border-color:#3f4753}
  .tagbtn.on{background:#16361f;color:#6ee7a0;border-color:#1f5132}
  .langtab{padding:4px 11px;font-size:12px;background:#23262c;color:#8b95a1;border:1px solid #313640;border-radius:14px;cursor:pointer}
  .langtab:hover{border-color:#3f4753}
  .langtab.has{color:#6ee7a0;border-color:#1f5132}
  .langtab.cur{background:#1d2734;border-color:#2563eb;color:#fff}
  .langtab .lp{font-size:9px;background:#2563eb;color:#fff;border-radius:6px;padding:0 4px;margin-left:5px;vertical-align:middle}
  .visgrp{display:flex;gap:4px;flex-wrap:wrap}
  .vbtn{padding:5px 12px;font-size:12px;background:#23262c;color:#8b95a1;border:1px solid #313640;border-radius:6px;cursor:pointer}
  .vbtn.on{background:#2563eb;color:#fff;border-color:#2563eb}
  .chk{display:inline-flex;align-items:center;gap:7px;cursor:pointer;font-size:13px;color:#e6e6e6}
  .chk input{width:auto;margin:0;accent-color:#2563eb}
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
    <option value="zh">中文</option>
  </select>
</div>
<div class="sub"><span id="l_game"></span>: <b id="gv"></b> · <span id="l_up"></span>: <span id="upx"></span> · <span id="l_hint"></span></div>
<div id="warn"></div>

<div class="wrap">
  <div class="listcol">
    <div class="lbar">
      <input id="q" oninput="renderList()">
      <div class="row2">
        <select id="filter" onchange="FILTER=this.value;renderList()">
          <option value="all"></option><option value="published"></option><option value="needs"></option><option value="unpublished"></option>
        </select>
        <select id="sort" onchange="SORT=this.value;renderList()">
          <option value="recent"></option><option value="name"></option><option value="installed"></option>
        </select>
      </div>
      <div class="lcount" id="lcount"></div>
    </div>
    <div class="list" id="list"></div>
  </div>
  <div class="detail" id="detail"></div>
</div>

<script>
const I18N={
 en:{title:"STS2 Steam Workshop Upload",gameInstall:"Game build",uploader:"Uploader",
  hint:"select a mod on the left to review and upload",sortLabel:"Sort",
  sortRecent:"Recently uploaded",sortName:"Name",sortInstalled:"Installed first",
  searchPh:"Filter by name…",filterAll:"All",filterPublished:"Published",filterNeeds:"Needs update",filterUnpub:"Unpublished",count:"{n} mods",
  warnNoUploader:"ModUploader.exe not found: ",warnSteam:"Steam does not appear to be running. Start Steam and log in before uploading.",
  emptyDetail:"Select a mod on the left.",badgeInstalled:"installed",badgeNotInstalled:"not installed",unpublished:"unpublished",
  never:"never",now:"just now",min:"m ago",hour:"h ago",day:"d ago",
  imgNone:"none (placeholder will be used)",imgPlaceholder:"⚠ placeholder (replace it)",imgOver:" · ⚠ over 1MB",
  packagable:"{n} files ready to package",notInstalled:"not installed — needs Build+Upload",workshop:"Workshop",
  chkTitle:"Title set",chkDesc:"Description set",chkImage:"Thumbnail set (not placeholder/over 1MB)",
  chkContent:"Content to upload (installed)",chkItem:"Workshop item exists",chkItemNew:"(will be created)",
  readyYes:"✔ Ready to publish",readyNo:"⚠ Some items missing",
  desc:"description",edit:"Edit",preview:"Preview",uploadAs:"Upload as",bbcode:"BBCode",plain:"Plain text",
  descBytesOk:"{n} / {max} bytes",
  descBytesNear:"{n} / {max} bytes · approaching Steam's per-language description limit",
  descBytesOver:"⚠ {n} / {max} bytes · over Steam's description limit — on upload this language is silently rejected (InvalidParam) and NOT registered. Shorten it. Note: CJK/Cyrillic take 2–3 bytes per character.",
  tagsPh:"tool, ...",thumbHint:"pick a file → auto-converted to PNG under 1MB",setBtn:"Set",
  reqItems:"required items",reqItemsPh:"workshop item IDs, comma-separated (e.g. 3752522987)",
  reqItemsHint:"Steam \"Required Items\". Leave empty: whatever is set on the Workshop page is preserved automatically and re-imported on every upload. Fill in only to override and manage them here.",
  reqItemsLive:"Auto-synced from Workshop (last upload):",
  langLabel:"languages",langDefault:"default",
  langNoteDefault:"Default (shown when a user's Steam language isn't provided): {lang}. Click a language to edit its title & description below. ✓ = provided; clear both fields to drop a language.",
  langSetPrimary:"make current language the default",
  wsIdPh:"existing Workshop item ID (leave empty to create new on Upload)",apply:"Apply",
  wsIdLinked:"linked locally → Upload updates this item",
  wsIdMissing:"no local mod_id — paste an existing ID to avoid a duplicate",
  saveCfg:"Save config",privateNote:"※ private — switch to public and Upload again when ready",
  creditFooter:"Credit",creditHint:"Optional — append a link to STS2 Mod Uploader UI at the end of the description.",
  logReady:"Ready. Steam must be running and logged in to upload.",
  busy:"[busy] another upload is in progress",cfgSaved:"[config] saved",
  imgPick:"[img] pick a file first",imgSet:"[img] set",imgFail:"[img] failed: ",
  modidLinked:"[modid] linked: ",modidRemoved:"[modid] removed (a new item will be created)",modidFail:"[modid] failed: ",
  doneOk:"✅ done",doneFail:"❌ failed: ",closed:"[connection closed]",
  needsUpdate:"Update needed",deployed:"deployed",current:"current"},
 ko:{title:"STS2 Steam Workshop 업로드",gameInstall:"게임 설치본",uploader:"업로더",
  hint:"목록의 모드를 선택하면 우측에서 검토·업로드",sortLabel:"정렬",
  sortRecent:"최신 업로드순",sortName:"이름순",sortInstalled:"설치 먼저",
  searchPh:"이름으로 검색…",filterAll:"전체",filterPublished:"게시됨",filterNeeds:"업데이트 필요",filterUnpub:"미게시",count:"{n}개",
  warnNoUploader:"ModUploader.exe 를 찾을 수 없습니다: ",warnSteam:"Steam 이 실행되지 않은 것 같습니다. 업로드 전에 Steam 을 켜고 로그인하세요.",
  emptyDetail:"왼쪽에서 모드를 선택하세요.",badgeInstalled:"설치됨",badgeNotInstalled:"미설치",unpublished:"미게시",
  never:"미업로드",now:"방금",min:"분 전",hour:"시간 전",day:"일 전",
  imgNone:"미등록 (placeholder 사용 예정)",imgPlaceholder:"⚠ placeholder (교체 필요)",imgOver:" · ⚠ 1MB 초과",
  packagable:"설치본 {n}개 패키징 가능",notInstalled:"미설치 — Build+Upload 필요",workshop:"워크샵",
  chkTitle:"타이틀 등록",chkDesc:"설명(BBCode) 등록",chkImage:"썸네일 등록 (placeholder/1MB초과 아님)",
  chkContent:"업로드할 content (설치본)",chkItem:"워크샵 아이템 존재",chkItemNew:"(신규 생성됨)",
  readyYes:"✔ 게시 준비 완료",readyNo:"⚠ 미충족 항목 있음",
  desc:"설명",edit:"편집",preview:"미리보기",uploadAs:"업로드 형식",bbcode:"BBCode",plain:"일반텍스트",
  descBytesOk:"{n} / {max} bytes",
  descBytesNear:"{n} / {max} bytes · Steam 언어별 설명 한도에 근접",
  descBytesOver:"⚠ {n} / {max} bytes · Steam 설명 한도 초과 — 업로드 시 이 언어는 조용히 거부(InvalidParam)되어 등록되지 않습니다. 줄여주세요. (한중일·키릴 문자는 글자당 2~3바이트)",
  tagsPh:"tool, ...",thumbHint:"파일 선택 → 1MB 이하 PNG 자동 변환",setBtn:"등록",
  reqItems:"필요한 아이템",reqItemsPh:"워크샵 아이템 ID, 쉼표로 구분 (예: 3752522987)",
  reqItemsHint:"Steam '필요한 아이템(Required Items)'. 비워두면 워크샵에 설정된 항목이 업로드 때마다 자동 유지·재반영됩니다(자동 가져오기). 입력하면 그 목록으로 직접 지정(덮어쓰기).",
  reqItemsLive:"워크샵에서 자동 동기화됨(마지막 업로드 기준):",
  langLabel:"언어",langDefault:"기본",
  langNoteDefault:"기본(사용자 Steam 언어가 없을 때 표시): {lang}. 언어를 클릭하면 아래 타이틀·설명이 그 언어로 바뀝니다. ✓ = 제공됨, 두 칸을 비우면 해당 언어 제거.",
  langSetPrimary:"현재 언어를 기본으로 지정",
  wsIdPh:"기존 워크샵 아이템 ID (없으면 비워두면 Upload 시 새로 생성)",apply:"적용",
  wsIdLinked:"로컬 연결됨 → Upload 시 이 아이템 갱신",
  wsIdMissing:"로컬 mod_id 없음 — 기존 아이템이 있으면 ID 입력(중복 생성 방지)",
  saveCfg:"config 저장",privateNote:"※ private — 확인 후 public 으로 바꿔 다시 Upload",
  creditFooter:"크레딧 표기",creditHint:"선택 — 설명 끝에 STS2 Mod Uploader UI 링크를 추가합니다.",
  logReady:"준비됨. 업로드 시 Steam 이 실행·로그인된 상태여야 합니다.",
  busy:"[busy] 다른 업로드가 진행 중",cfgSaved:"[config] 저장됨",
  imgPick:"[img] 파일을 선택하세요",imgSet:"[img] 등록됨",imgFail:"[img] 실패: ",
  modidLinked:"[modid] 연결: ",modidRemoved:"[modid] 제거됨(새 아이템 생성 예정)",modidFail:"[modid] 실패: ",
  doneOk:"✅ 완료",doneFail:"❌ 실패: ",closed:"[연결 종료]",
  needsUpdate:"업데이트 필요",deployed:"배포",current:"현재"},
 zh:{title:"STS2 Steam 创意工坊上传",gameInstall:"游戏版本",uploader:"上传器",
  hint:"在左侧选择一个模组以查看并上传",sortLabel:"排序",
  sortRecent:"最近上传",sortName:"名称",sortInstalled:"已安装优先",
  searchPh:"按名称筛选…",filterAll:"全部",filterPublished:"已发布",filterNeeds:"需要更新",filterUnpub:"未发布",count:"{n} 个模组",
  warnNoUploader:"未找到 ModUploader.exe： ",warnSteam:"Steam 似乎未运行。上传前请启动 Steam 并登录。",
  emptyDetail:"请在左侧选择一个模组。",badgeInstalled:"已安装",badgeNotInstalled:"未安装",unpublished:"未发布",
  never:"从未",now:"刚刚",min:"分钟前",hour:"小时前",day:"天前",
  imgNone:"无（将使用占位图）",imgPlaceholder:"⚠ 占位图（请替换）",imgOver:" · ⚠ 超过 1MB",
  packagable:"{n} 个文件可打包",notInstalled:"未安装 — 需要 Build+Upload",workshop:"创意工坊",
  chkTitle:"标题已设置",chkDesc:"描述已设置",chkImage:"缩略图已设置（非占位图/未超 1MB）",
  chkContent:"待上传内容（已安装）",chkItem:"创意工坊项目存在",chkItemNew:"（将会创建）",
  readyYes:"✔ 可以发布",readyNo:"⚠ 有缺失项",
  desc:"描述",edit:"编辑",preview:"预览",uploadAs:"上传格式",bbcode:"BBCode",plain:"纯文本",
  descBytesOk:"{n} / {max} bytes",
  descBytesNear:"{n} / {max} bytes · 接近 Steam 单语言描述上限",
  descBytesOver:"⚠ {n} / {max} bytes · 超过 Steam 描述上限 — 上传时该语言会被静默拒绝（InvalidParam）且不会注册。请缩短。（中日韩/西里尔字符每个约占 2–3 字节）",
  tagsPh:"tool, ...",thumbHint:"选择文件 → 自动转换为 1MB 以下的 PNG",setBtn:"设置",
  reqItems:"必需项目",reqItemsPh:"创意工坊项目 ID，用逗号分隔（例如 3752522987）",
  reqItemsHint:"Steam “必需项目（Required Items）”。留空：创意工坊页面上已设置的项目会自动保留，并在每次上传时重新导入。仅在需要覆盖并在此管理时填写。",
  reqItemsLive:"已从创意工坊自动同步（上次上传）：",
  langLabel:"语言",langDefault:"默认",
  langNoteDefault:"默认（当用户的 Steam 语言不可用时显示）：{lang}。点击某语言即可在下方编辑其标题和描述。✓ = 已提供；清空两个字段可移除该语言。",
  langSetPrimary:"将当前语言设为默认",
  wsIdPh:"现有创意工坊项目 ID（留空则在上传时新建）",apply:"应用",
  wsIdLinked:"已在本地关联 → 上传将更新此项目",
  wsIdMissing:"没有本地 mod_id — 若已有项目请粘贴其 ID 以避免重复",
  saveCfg:"保存配置",privateNote:"※ 私密 — 准备好后切换为公开并再次上传",
  creditFooter:"署名",creditHint:"可选 — 在描述末尾附加 STS2 Mod Uploader UI 链接。",
  logReady:"就绪。上传时 Steam 必须处于运行并已登录状态。",
  busy:"[busy] 另一个上传正在进行中",cfgSaved:"[config] 已保存",
  imgPick:"[img] 请先选择文件",imgSet:"[img] 已设置",imgFail:"[img] 失败： ",
  modidLinked:"[modid] 已关联： ",modidRemoved:"[modid] 已移除（将创建新项目）",modidFail:"[modid] 失败： ",
  doneOk:"✅ 完成",doneFail:"❌ 失败： ",closed:"[连接已关闭]",
  needsUpdate:"需要更新",deployed:"已部署",current:"当前"}
};
let MODS=[], SEL=null, busy=false, SORT='recent', FILTER='all';
let LANG=localStorage.getItem('lang')||(l=>l.startsWith('ko')?'ko':(l.startsWith('zh')?'zh':'en'))((navigator.language||'').toLowerCase());
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
  $('#q').placeholder=t('searchPh');
  const fo=$('#filter').options; fo[0].textContent=t('filterAll'); fo[1].textContent=t('filterPublished'); fo[2].textContent=t('filterNeeds'); fo[3].textContent=t('filterUnpub');
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
  if(!SEL){ const last=localStorage.getItem('lastMod'); if(last && MODS.some(x=>x.id===last)) SEL=last; }
  applyLang();
}
function renderWarn(){
  const w=$('#warn'); if(!w) return; w.innerHTML='';
  if(!STATE.uploader_ok) w.innerHTML+='<div class="warn">'+esc(t('warnNoUploader'))+esc(STATE.uploader_exe||'')+'</div>';
  if(STATE.steam_running===false) w.innerHTML+='<div class="warn">'+esc(t('warnSteam'))+'</div>';
}
function dotClass(m){ if(m.checks.ready) return 'ready'; if(m.checks.title&&m.checks.description) return 'partial'; return 'bad'; }
function visibleMods(){
  const q=(($('#q')&&$('#q').value)||'').toLowerCase().trim();
  return sortMods().filter(m=>{
    if(FILTER==='published'   && !m.workshop_item_id) return false;
    if(FILTER==='needs'       && !m.needs_update)     return false;
    if(FILTER==='unpublished' &&  m.workshop_item_id) return false;
    if(q && !(m.name.toLowerCase().includes(q) || m.id.toLowerCase().includes(q))) return false;
    return true;
  });
}
function renderList(){
  const L=$('#list'); L.innerHTML='';
  const list=visibleMods();
  const c=$('#lcount'); if(c) c.textContent=t('count').replace('{n}',list.length);
  for(const m of list){
    const div=document.createElement('div');
    div.className='li'+(m.id===SEL?' sel':'');
    div.innerHTML=`<span class="dot ${dotClass(m)}" title="ready=${m.checks.ready}"></span>
      <span class="nm"><b>${esc(m.name)}</b><span>${esc(m.version)} · ${m.workshop_item_id?esc(relTime(m.last_upload)):esc(t('unpublished'))}</span></span>
      ${m.needs_update?'<span class="badge upd" title="'+esc(t('needsUpdate'))+'">⟳</span>':''}
      ${!m.installed?'<span class="badge no">'+esc(t('badgeNotInstalled'))+'</span>':''}
      ${m.workshop_item_id?'<span class="badge muted" title="'+esc(t('filterPublished'))+'">●</span>':''}`;
    div.onclick=()=>{SEL=m.id;localStorage.setItem('lastMod',m.id);renderList();renderDetail(m);};
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
      <label>${esc(t('langLabel'))}</label>
      <div>
        <div class="tagbtns" id="langtabs"></div>
        <div class="sub" id="langnote" style="margin-top:5px"></div>
      </div>
      <label>title</label><input id="f_title" value="${esc(m.cfg.title)}" oninput="langTitleInput()">
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
        <textarea id="f_desc" rows="12" oninput="langDescInput()">${esc(m.cfg.description)}</textarea>
        <div id="f_preview" class="preview" style="display:none"></div>
        <div id="f_bytes" class="sub" style="margin-top:4px"></div>
      </div>
      <label>tags</label>
      <div>
        <input id="f_tags" value="${esc((m.cfg.tags||[]).join(', '))}" placeholder="${esc(t('tagsPh'))}" oninput="syncTagButtons()">
        <div class="tagbtns" id="tagbtns"></div>
      </div>
      <label>${esc(t('reqItems'))}</label>
      <div>
        <input id="f_deps" value="${esc((m.cfg.dependencies||[]).join(', '))}" placeholder="${esc(t('reqItemsPh'))}">
        <div class="sub" style="margin-top:4px">${esc(t('reqItemsHint'))}</div>
        ${(m.cfg.dependenciesLive&&m.cfg.dependenciesLive.length)?'<div class="sub" style="margin-top:4px;color:#6ee7a0">'+esc(t('reqItemsLive'))+' '+m.cfg.dependenciesLive.map(x=>esc(String(x))).join(', ')+'</div>':''}
      </div>
      <label>changeNote</label><textarea id="f_note" rows="2">${esc(noteVal)}</textarea>
      <label>visibility</label>
      <div class="visgrp" id="f_vis">
        ${['private','public','unlisted','friends_only'].map(v=>`<button type="button" class="vbtn${m.cfg.visibility===v?' on':''}" data-vis="${v}" onclick="setVis('${v}')">${v}</button>`).join('')}
      </div>
      <label>credit</label>
      <div>
        <label class="chk"><input type="checkbox" id="f_credit" onchange="window.CREDIT=this.checked;updateDescBytes()"> <span id="f_credit_label"></span></label>
        <div class="sub" id="f_credit_hint" style="margin-top:4px"></div>
      </div>
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
      <span class="sub" id="privHint" style="${m.cfg.visibility==='private'?'':'display:none'}">${esc(t('privateNote'))}</span>
    </div>
    <div id="log">${esc(t('logReady'))}\n</div>`;

  D.querySelectorAll('[data-bb]').forEach(b=>b.onclick=e=>{e.preventDefault();bb($('#f_desc'),b.dataset.bb);});
  window.DVIEW='edit'; window.DMODE=m.cfg.descMode||'bbcode'; applyDescUI();
  window.VIS=m.cfg.visibility; window.CREDIT=!!m.cfg.creditFooter;
  applyCreditBtn();
  renderTagButtons();
  // 언어별 타이틀/설명을 하나의 맵으로 보관. 기본 언어 + 추가 언어 모두 포함.
  window.LANGDATA={};
  window.PRIMARYLANG=m.cfg.language||'english';
  window.LANGDATA[window.PRIMARYLANG]={title:m.cfg.title||'',description:m.cfg.description||''};
  (m.cfg.localizations||[]).forEach(L=>{ if(L.language) window.LANGDATA[L.language]={title:L.title||'',description:L.description||''}; });
  window.CURLANG=window.PRIMARYLANG;
  renderLangTabs(); updateLangNote();
}
function langHas(c){const d=window.LANGDATA&&window.LANGDATA[c];return !!(d&&((d.title&&d.title.trim())||(d.description&&d.description.trim())));}
function renderLangTabs(){
  const box=$('#langtabs'); if(!box) return;
  box.innerHTML=LANGS.map(([c,n])=>{
    const cls='langtab'+(c===window.CURLANG?' cur':'')+(langHas(c)?' has':'');
    const badge=(c===window.PRIMARYLANG)?' <span class="lp">'+esc(t('langDefault'))+'</span>':'';
    return `<button type="button" class="${cls}" onclick="selectLang('${c}')">${langHas(c)?'✓ ':''}${esc(n)}${badge}</button>`;
  }).join('');
}
function saveCurLang(){
  if(!window.CURLANG||!window.LANGDATA) return;
  window.LANGDATA[window.CURLANG]={title:($('#f_title')?$('#f_title').value:''),description:($('#f_desc')?$('#f_desc').value:'')};
}
function selectLang(c){
  saveCurLang();
  window.CURLANG=c;
  if(!window.LANGDATA[c]) window.LANGDATA[c]={title:'',description:''};
  const d=window.LANGDATA[c];
  if($('#f_title')) $('#f_title').value=d.title||'';
  if($('#f_desc'))  $('#f_desc').value=d.description||'';
  applyDescUI(); renderLangTabs(); updateLangNote();
}
function langTitleInput(){ if(window.LANGDATA&&window.CURLANG) window.LANGDATA[window.CURLANG].title=$('#f_title').value; renderLangTabs(); }
function langDescInput(){ if(window.LANGDATA&&window.CURLANG) window.LANGDATA[window.CURLANG].description=$('#f_desc').value; if(window.DVIEW==='preview') applyDescUI(); renderLangTabs(); updateDescBytes(); }
function setPrimaryLang(){ saveCurLang(); window.PRIMARYLANG=window.CURLANG; renderLangTabs(); updateLangNote(); }
function updateLangNote(){
  const el=$('#langnote'); if(!el) return;
  let html=esc(t('langNoteDefault')).replace('{lang}','<b>'+esc(langName(window.PRIMARYLANG))+'</b>');
  if(window.CURLANG&&window.CURLANG!==window.PRIMARYLANG)
    html+=' · <a href="#" onclick="setPrimaryLang();return false">'+esc(t('langSetPrimary'))+'</a>';
  el.innerHTML=html;
}
function setVis(v){
  window.VIS=v;
  document.querySelectorAll('#f_vis .vbtn').forEach(b=>b.classList.toggle('on',b.dataset.vis===v));
  const ph=$('#privHint'); if(ph) ph.style.display=(v==='private')?'':'none';
}
function applyCreditBtn(){
  const cb=$('#f_credit'); if(cb) cb.checked=!!window.CREDIT;
  const l=$('#f_credit_label'); if(l) l.textContent=t('creditFooter');
  const h=$('#f_credit_hint'); if(h) h.textContent=t('creditHint');
}
// Steam API 언어 코드(value) → 표시명. 워크샵 언어별 타이틀/설명용.
const LANGS=[["english","English"],["koreana","한국어"],["schinese","简体中文"],["tchinese","繁體中文"],
  ["japanese","日本語"],["french","Français"],["german","Deutsch"],["spanish","Español"],["latam","Español (LATAM)"],
  ["russian","Русский"],["brazilian","Português (BR)"],["portuguese","Português"],["italian","Italiano"],
  ["polish","Polski"],["turkish","Türkçe"],["thai","ไทย"],["ukrainian","Українська"],["vietnamese","Tiếng Việt"]];
function langName(c){const e=LANGS.find(x=>x[0]===c);return e?e[1]:c;}
// 자주 쓰는 태그 프리셋 (수정 가능). 텍스트 입력이 source of truth, 버튼은 토글만.
const TAG_PRESETS=["Gameplay","QoL","UI","Cosmetic","Tools","Balance","Cards","Relics","Characters","Localization","Performance"];
function currentTags(){return (($('#f_tags')&&$('#f_tags').value)||'').split(',').map(s=>s.trim()).filter(Boolean);}
function renderTagButtons(){
  const box=$('#tagbtns'); if(!box) return;
  box.innerHTML=TAG_PRESETS.map(tg=>`<button type="button" class="tagbtn" data-tag="${esc(tg)}" onclick="toggleTag('${esc(tg)}')">${esc(tg)}</button>`).join('');
  syncTagButtons();
}
function syncTagButtons(){
  const active=currentTags().map(s=>s.toLowerCase());
  document.querySelectorAll('#tagbtns .tagbtn').forEach(b=>b.classList.toggle('on',active.includes(b.dataset.tag.toLowerCase())));
}
function toggleTag(tag){
  const arr=currentTags(), i=arr.findIndex(x=>x.toLowerCase()===tag.toLowerCase());
  if(i>=0) arr.splice(i,1); else arr.push(tag);
  $('#f_tags').value=arr.join(', '); syncTagButtons();
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
  updateDescBytes();
}
// Steam caps each language's description at 8000 UTF-8 bytes; an over-limit
// localization is silently rejected (k_EResultInvalidParam) on upload. Show a
// live byte count (incl. the credit footer that gets appended at save time) so
// the limit is visible per language before publishing.
const DESC_BYTE_LIMIT=8000;
const _CREDIT_BB="\n\n[hr][/hr]\n[i]Published with [url=https://github.com/ing-gom/sts2-mod-uploader-ui]STS2 Mod Uploader UI[/url].[/i]";
const _CREDIT_TXT="\n\nPublished with STS2 Mod Uploader UI — https://github.com/ing-gom/sts2-mod-uploader-ui";
function utf8Bytes(s){return new TextEncoder().encode(s||'').length;}
function updateDescBytes(){
  const ta=$('#f_desc'), el=$('#f_bytes'); if(!ta||!el) return;
  let s=ta.value||'';
  if(window.CREDIT) s+=(window.DMODE==='plain'?_CREDIT_TXT:_CREDIT_BB);
  const n=utf8Bytes(s), max=DESC_BYTE_LIMIT;
  const key = n>max ? 'descBytesOver' : (n>max-400 ? 'descBytesNear' : 'descBytesOk');
  el.textContent=t(key).replace('{n}',n).replace('{max}',max);
  el.style.color = n>max ? '#ff6b6b' : (n>max-400 ? '#e0b341' : '');
  el.style.fontWeight = n>max ? '600' : '';
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
  saveCurLang();  // 현재 보고 있는 언어의 편집 내용을 LANGDATA 에 반영
  const prim=window.PRIMARYLANG||'english';
  const pd=(window.LANGDATA&&window.LANGDATA[prim])||{title:'',description:''};
  m.cfg.language=prim;
  m.cfg.title=pd.title||'';
  m.cfg.description=pd.description||'';
  m.cfg.localizations=Object.keys(window.LANGDATA||{})
    .filter(c=>c!==prim)
    .map(c=>({language:c,title:window.LANGDATA[c].title||'',description:window.LANGDATA[c].description||''}))
    .filter(L=>(L.title&&L.title.trim())||(L.description&&L.description.trim()));
  m.cfg.tags=$('#f_tags').value.split(',').map(s=>s.trim()).filter(Boolean);
  m.cfg.dependencies=(($('#f_deps')&&$('#f_deps').value)||'').split(/[\s,]+/).map(s=>s.trim()).filter(s=>/^\d+$/.test(s)).map(Number);
  m.cfg.changeNote=$('#f_note').value;
  m.cfg.visibility=window.VIS||m.cfg.visibility;
  m.cfg.descMode=window.DMODE||'bbcode';
  m.cfg.creditFooter=!!($('#f_credit')&&$('#f_credit').checked);
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
