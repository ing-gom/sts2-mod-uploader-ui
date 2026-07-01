# STS2 Mod Uploader UI

A small **local web dashboard** for publishing & updating your *Slay the Spire 2* mods to the Steam Workshop. It wraps the official [`ModUploader.exe`](https://github.com/MegaCrit/sts2-mod-uploader) (or any STS2 mod uploader CLI) with a UI so you can review and upload from one screen instead of editing JSON and running commands by hand.

**🔗 Project page: https://ing-gom.github.io/sts2-mod-uploader-ui/**

> 🇰🇷 한국어 안내는 아래 [한국어](#한국어) 참고.

<!-- 스크린샷을 docs/screenshot.png 에 추가하면 여기 표시됩니다 -->

## What it does

- **Scans your game's `mods/` folder** and lists every installed mod (auto-detects the Steam install via the registry + `libraryfolders.vdf`).
- **Master–detail UI** — pick a mod on the left, review it on the right.
- **Registration checklist** — shows at a glance whether the **title / description / thumbnail / content / workshop item** are set (`✔ / ✘`) before you publish.
- **Thumbnail** — pick an image file; it's auto-converted to PNG and shrunk **under Steam's 1 MB preview limit**.
- **Description** — BBCode editor with insert buttons + an **Edit / Preview** toggle that renders BBCode the way Steam will show it. Optional plain-text mode.
- **Multiple languages** — language chips above the title; click one to edit that language's title & description in the same editor (`✓` marks the languages you've provided). Each Steam user sees the page in their own language, falling back to the default. *(requires the patched uploader — see below)*
- **Required items are preserved** — the workshop's existing *Required Items* (dependencies) are kept automatically on every upload and shown in the dashboard, instead of being wiped. *(requires the patched uploader — see below)*
- **Workshop item ID** field — pre-filled if known; paste an existing ID to update that item instead of creating a duplicate.
- **One-click upload** — packages the installed mod folder (skipping runtime junk like `*.preset` / `*.log`) and runs the uploader, streaming its output live.
- **Build + Upload** (optional) — for mods you map to a `.csproj` in `config.json`, runs `dotnet build -c Release` first.
- Per-mod state (workshop.json, `mod_id.txt`) is kept in `.workshop/<ModId>/`, so updates always target the same Workshop item.

## Requirements

- **Windows** with **Steam running and logged in** (the uploader uses the Steam API; it can't run headless).
- **Python 3.8+** with **[Pillow](https://pypi.org/project/Pillow/)** (`pip install pillow`) for thumbnail processing.
- An **STS2 mod uploader** (`ModUploader.exe` + `steam_api64.dll` + `steam_appid.txt`). Two choices:
  - **Patched build (recommended)** — download `ModUploader-patched-win-x64.zip` from this repo's [Releases](https://github.com/ing-gom/sts2-mod-uploader-ui/releases). It adds **multi-language title/description** and **keeps existing Required Items** instead of wiping them. See [Patched uploader](#patched-uploader) below.
  - **Official build** — the upstream [`ModUploader.exe`](https://github.com/MegaCrit/sts2-mod-uploader). Works fine for normal uploads, but ignores the multi-language fields and clears Required Items on every upload.
  - Either way, put the `ModUploader-win-x64` folder next to this script, **or** set `uploader_dir` in `config.json`, **or** set the `STS2_UPLOADER_DIR` environment variable.

## Usage

```sh
pip install pillow
python workshop_dashboard.py
```

Then open the printed URL (default `http://127.0.0.1:8791`). Select a mod, fill in the title/description/thumbnail, set visibility to **private** for a first test, and hit **Upload**. Verify it on Steam, then switch to **public** and upload again.

> ⚠️ Uploading **creates a real Steam Workshop item** under your account. Start with `private`.

### One-click launch (Windows)

Don't want to type the command each time? Two extras are included:

<p align="left"><img src="icon.png" alt="launcher icon" width="72"></p>

- **`launch.bat`** — double-click it to start the dashboard and open your browser automatically. It finds Python on its own (`py`/`python`) and runs from wherever the repo lives, so no path editing is needed.
- **`Create Desktop Shortcut.bat`** — double-click once to drop a **STS2 Mod Uploader** icon on your Desktop (pointing at `launch.bat`, with the icon above). After that, launch the dashboard any time from that icon.

The Desktop icon is generated from `icon.ico`, which ships in the repo. To regenerate or customize it, run `python make_icon.py` (uses Pillow, already required).

## Configuration (optional)

Copy `config.example.json` to `config.json` and fill in only what you need — everything is optional:

| key | meaning |
|---|---|
| `uploader_dir` | folder containing `ModUploader.exe` |
| `game_path` | STS2 install folder (skips auto-detect) |
| `workspaces` | where per-mod workspaces live (default `.workshop/`) |
| `port` | dashboard port (default 8791, auto-increments if busy) |
| `sources` | `{ "<ModId>": "path/to/Mod.csproj" }` to enable **Build + Upload** for that mod |
| `exclude` | extra glob patterns to drop from uploaded content |

Equivalent env vars: `STS2_UPLOADER_DIR`, `STS2_PATH`, `STS2_WORKSPACES`, `STS2_DASH_PORT`.

## Claude Code skill

This repo ships a [`workshop-upload`](.claude/skills/workshop-upload/SKILL.md) skill. If you
open the repo in [Claude Code](https://claude.com/claude-code), you can just ask it to
*"upload my mod to the Workshop as private"* and it drives the pipeline for you (sets the
title/description/thumbnail, then runs the uploader and reports the item link). Steam still
has to be running and logged in.

## Patched uploader

The multi-language and required-items features need a small patch to the uploader CLI, because the official build doesn't expose them. A prebuilt, self-contained Windows build is attached to this repo's [Releases](https://github.com/ing-gom/sts2-mod-uploader-ui/releases) as `ModUploader-patched-win-x64.zip` — unzip it and point the dashboard at it (next to the script, or via `uploader_dir`).

It's a fork of the official MIT-licensed [MegaCrit/sts2-mod-uploader](https://github.com/MegaCrit/sts2-mod-uploader) with three source changes:

- **Required items aren't wiped** — if `workshop.json` has no `dependencies` key, existing Required Items on the Workshop are left untouched (the official build reconciles to an empty list and removes them). When you *do* list dependencies, they're applied exactly.
- **Localized title/description** — `language` (primary) + a `localizations` list are submitted as per-language metadata updates via `SetItemUpdateLanguage`, so each language gets its own title/description.
- **Live required-items sync** — after upload, the current Required Items are written back into `workshop.json` (`dependenciesLive`) so the dashboard can display them.

The patch is also proposed upstream; if it lands in the official tool, you can switch back to the official build.

## Notes / gotchas

- **`mod_id.txt` is important.** It links a workspace to its Workshop item. If you lose it (e.g. fresh clone), the dashboard shows the mod as "new" and an upload would create a **duplicate** — paste the existing ID into the *workshop ID* field first.
- The dashboard knows an item "exists" from the local `mod_id.txt`, not from a live Steam query (the CLI doesn't offer one).
- Steam preview images must be **≤ 1 MB**; the thumbnail step handles this for you.

---

## 한국어

*Slay the Spire 2* 모드를 Steam 창작마당에 올리고 갱신하는 **로컬 웹 대시보드**입니다. 공식 `ModUploader.exe` CLI를 UI로 감싸, JSON 편집·명령어 실행 없이 한 화면에서 검토하고 업로드합니다.

**기능**: 게임 `mods/` 폴더 자동 스캔(레지스트리+`libraryfolders.vdf`로 Steam 경로 감지) · 좌측 목록/우측 상세 · 등록 검증 체크리스트(타이틀/설명/썸네일/content/아이템) · 썸네일 파일 선택→1MB 이하 PNG 자동 변환 · BBCode 편집 + **편집/미리보기**(스팀처럼 렌더링) · **다국어**(타이틀 위 언어 칩, 클릭 시 그 언어의 타이틀/설명 편집, ✓=제공됨) · **필요한 아이템(Required Items) 자동 보존**(업로드해도 초기화되지 않음) · 워크샵 ID 입력(중복 생성 방지) · 원클릭 업로드(런타임 부산물 제외) + 실시간 로그 · 선택적 Build+Upload(`config.json`의 `sources`에 csproj 매핑 시). ※다국어·필요한 아이템 보존은 **패치된 업로더** 필요(아래).

**필요**: Windows + **Steam 실행/로그인**(헤드리스 불가), Python 3.8+ & `pip install pillow`, STS2 업로더. **패치 빌드(권장)** = 이 repo [Releases](https://github.com/ing-gom/sts2-mod-uploader-ui/releases)의 `ModUploader-patched-win-x64.zip`(다국어 + 필요한 아이템 보존 추가, 공식 [MegaCrit/sts2-mod-uploader](https://github.com/MegaCrit/sts2-mod-uploader) MIT 포크). 공식 빌드도 일반 업로드는 되지만 다국어 무시 + 필요한 아이템 초기화. 업로더 위치는 스크립트 옆에 두거나 `config.json`의 `uploader_dir` / `STS2_UPLOADER_DIR`로 지정.

**실행**: `pip install pillow` 후 `python workshop_dashboard.py` → 안내 URL 열기 → 모드 선택 → 타이틀/설명/썸네일 입력 → 처음엔 `private`로 Upload → 확인 후 `public`. ⚠️ 업로드는 **실제 창작마당 아이템을 생성**하므로 처음엔 private 권장. `mod_id.txt`는 아이템 연결 정보라 잃으면 중복 생성 위험 → 워크샵 ID 칸에 기존 ID 입력.

**원클릭 실행(Windows)**: 매번 명령어 치기 번거로우면 — **`launch.bat`** 더블클릭 = 대시보드 실행 + 브라우저 자동 오픈(Python 자동 탐지, 경로 하드코딩 없음). **`Create Desktop Shortcut.bat`** 한 번 더블클릭 = 바탕화면에 **STS2 Mod Uploader** 아이콘 생성 → 이후 그 아이콘으로 실행. 아이콘은 repo의 `icon.ico`이며 `python make_icon.py`로 재생성/수정 가능.

## License

[MIT](LICENSE)

This is an independent UI wrapper and is not affiliated with Mega Crit.
