#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Chatwork → WordPress 自動投稿

専用の Chatwork 部屋に「写真1枚 + テンプレ」を投稿するだけで、
HP(WordPress)の NES PHOTOS / NEWS が自動更新される仕組み。

- photos 部屋: 写真 + 4行テンプレ → 通常投稿(post) を作成（NES PHOTOS 番号は自動採番）
- news   部屋: タイトル + 本文(+写真任意) → news 投稿を作成
- 新着メッセージは message_id を state.json で管理（重複投稿ゼロ）
- 初回はサイレント記録（既存メッセージを一気に投稿しない）
- 投稿後は同じ部屋へ「✅ 公開しました + URL」を返信

依存ライブラリなし（Python標準ライブラリのみ）。
GitHub Actions で定期実行する想定。

環境変数（GitHub Secrets 推奨）:
    CHATWORK_TOKEN    Chatwork APIトークン
    WP_USER           WordPress ユーザー名（例: sohei）
    WP_APP_PASSWORD   WordPress アプリケーションパスワード
ローカル実行時は config.local.json に同名キーで書いてもよい。

使い方:
    python sync.py                 # 新着を処理して投稿
    python sync.py --dry-run       # WordPressには書き込まず、解析結果だけ表示
    python sync.py --init          # 既存メッセージを「処理済み」にして初期化（投稿しない）
    python sync.py --channel photos  # 片方の部屋だけ処理
    python sync.py --force-all      # 未処理判定を無視して全メッセージを再処理（テスト用）
"""
import argparse
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from base64 import b64encode
from datetime import datetime, timezone, timedelta

try:  # Windowsコンソール(cp932)でも日本語を化けさせない
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

JST = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOCAL_CONFIG_PATH = os.path.join(HERE, "config.local.json")
STATE_PATH = os.path.join(HERE, "state.json")
CW_API = "https://api.chatwork.com/v2"

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic")
MAX_PROCESSED_KEEP = 500  # state に残す処理済みID数の上限（肥大化防止）


def log(*a):
    print("[sync]", *a, flush=True)


def jst_now():
    return datetime.now(JST)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log("読み込み失敗:", path, e)
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ============================================================
# 設定 / シークレット
# ============================================================

def get_secret(cfg_local, name):
    return os.environ.get(name) or (cfg_local or {}).get(name)


# ============================================================
# Chatwork API
# ============================================================

def cw_request(method, path, token, data=None, headers=None, raw=False):
    url = f"{CW_API}{path}"
    h = {"X-ChatWorkToken": token}
    if headers:
        h.update(headers)
    body = None
    if data is not None and not raw:
        body = urllib.parse.urlencode(data).encode()
    elif raw:
        body = data
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        status = r.status
        payload = r.read()
        if status == 204 or not payload:
            return None
        return json.loads(payload.decode("utf-8", "replace"))


def cw_get_messages(room_id, token, force=True):
    """部屋のメッセージ最大100件。新着なし(204)のときは空リスト。"""
    q = "?force=1" if force else ""
    res = cw_request("GET", f"/rooms/{room_id}/messages{q}", token)
    return res or []


def cw_list_files(room_id, token):
    res = cw_request("GET", f"/rooms/{room_id}/files", token)
    return res or []


def cw_file_download_url(room_id, file_id, token):
    res = cw_request("GET",
                     f"/rooms/{room_id}/files/{file_id}?create_download_url=1",
                     token)
    return (res or {}).get("download_url")


def http_download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (cw-to-wp)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def cw_post_message(room_id, body, token):
    return cw_request("POST", f"/rooms/{room_id}/messages", token, data={"body": body})


# ============================================================
# Chatwork メッセージ本文のクリーニング / テンプレ解析
# ============================================================

CW_TAG_PATTERNS = [
    re.compile(r"\[rp\b[^\]]*\].*?\[/rp\]", re.S),
    re.compile(r"\[qt\b[^\]]*\].*?\[/qt\]", re.S),
    re.compile(r"\[download:\d+\][^\[]*\[/download\]", re.S),
    re.compile(r"\[info\]|\[/info\]|\[title\]|\[/title\]", re.S),
    re.compile(r"\[To:\d+\]", re.S),
    re.compile(r"\[piconname:\d+\]|\[picon:\d+\]", re.S),
    re.compile(r"\[preview\b[^\]]*\]", re.S),
    re.compile(r"\(.*?を確認できます\)", re.S),
]


def clean_body(body):
    s = body or ""
    for pat in CW_TAG_PATTERNS:
        s = pat.sub("", s)
    # 残ったChatwork系タグ [xxx] / [xxx:yyy] を除去
    # （[dtext:file_uploaded] や [download:123]、[info] 等。日本語の[特価]等ASCII以外は残す）
    s = re.sub(r"\[/?[a-zA-Z]+(:[^\]]+)?\]", "", s)
    return s.strip()


def split_lines(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


# ラベル同義語 → 内部キー
PHOTO_LABELS = {
    "魚種": "species", "魚": "species", "species": "species", "fish": "species",
    "場所": "location", "エリア": "location", "産地": "location",
    "location": "location", "area": "location", "river": "location",
    "ロッド": "rod", "竿": "rod", "rod": "rod",
    "ライン": "line", "タックル": "line", "line": "line", "tackle": "line",
    "リーダー": "line",
}

LABEL_RE = re.compile(r"^\s*([^\s:：]+)\s*[:：]\s*(.*)$")


def parse_photo(body):
    """写真投稿テンプレを解析。
    ラベル形式（魚種:/場所:/ロッド:/ライン:）でも、ラベル無しの4行でもOK。
    返り値 dict {species, location, rod, line, extras[]} / 解析不能なら None。
    species は必須（タイトルに使う）。
    """
    text = clean_body(body)
    lines = split_lines(text)
    if not lines:
        return None

    fields = {"species": "", "location": "", "rod": "", "line": ""}
    extras = []
    used_labels = False

    for ln in lines:
        m = LABEL_RE.match(ln)
        if m and m.group(1) in PHOTO_LABELS:
            used_labels = True
            key = PHOTO_LABELS[m.group(1)]
            val = m.group(2).strip()
            if fields[key]:
                fields[key] += " " + val
            else:
                fields[key] = val

    if not used_labels:
        # ラベル無し → 上から順に species/location/rod/line、5行目以降は extras
        order = ["species", "location", "rod", "line"]
        for i, ln in enumerate(lines):
            if i < len(order):
                fields[order[i]] = ln
            else:
                extras.append(ln)
    else:
        # ラベル付きで、ラベルに該当しなかった行は extras 扱い
        for ln in lines:
            m = LABEL_RE.match(ln)
            if not (m and m.group(1) in PHOTO_LABELS):
                extras.append(ln)

    if not fields["species"]:
        return None
    fields["extras"] = extras
    return fields


NEWS_LABELS = {
    "タイトル": "title", "件名": "title", "title": "title", "見出し": "title",
    "本文": "body", "内容": "body", "body": "body", "text": "body",
    "リンク": "link", "url": "link", "link": "link",
}


def parse_news(body):
    """NEWSテンプレを解析。
    ラベル形式（タイトル:/本文:/リンク:）でも、ラベル無し（1行目=タイトル、残り=本文）でもOK。
    返り値 dict {title, body, link} / 解析不能なら None。title 必須。
    """
    text = clean_body(body)
    raw_lines = [ln.rstrip() for ln in text.splitlines()]
    # 先頭・末尾の空行を落とす
    while raw_lines and not raw_lines[0].strip():
        raw_lines.pop(0)
    while raw_lines and not raw_lines[-1].strip():
        raw_lines.pop()
    if not raw_lines:
        return None

    has_label = any(
        (LABEL_RE.match(ln) and LABEL_RE.match(ln).group(1) in NEWS_LABELS)
        for ln in raw_lines if ln.strip()
    )

    result = {"title": "", "body": "", "link": ""}
    if has_label:
        current = None
        buf = []

        def flush():
            if current:
                joined = "\n".join(buf).strip()
                if result[current]:
                    result[current] += "\n" + joined
                else:
                    result[current] = joined

        for ln in raw_lines:
            m = LABEL_RE.match(ln)
            if m and m.group(1) in NEWS_LABELS:
                flush()
                current = NEWS_LABELS[m.group(1)]
                buf = [m.group(2)]
            else:
                buf.append(ln)
        flush()
    else:
        nonempty = [ln for ln in raw_lines if ln.strip()]
        result["title"] = nonempty[0].strip()
        result["body"] = "\n".join(nonempty[1:]).strip()

    if not result["title"]:
        return None
    return result


# ============================================================
# WordPress REST API
# ============================================================

class WP:
    def __init__(self, base_url, user, app_password):
        self.base = base_url.rstrip("/")
        self.auth = "Basic " + b64encode(
            f"{user}:{app_password}".encode("utf-8")).decode("ascii")

    def _req(self, method, path, json_body=None, raw=None, extra_headers=None):
        url = f"{self.base}/wp-json/wp/v2{path}"
        headers = {"Authorization": self.auth}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            data = raw
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = r.read()
            return json.loads(payload.decode("utf-8", "replace")) if payload else {}

    def upload_media(self, filename, content, mime=None):
        mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        # filename はASCII安全に（日本語ファイル名でのヘッダ崩れ防止）
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "photo.jpg"
        headers = {
            "Content-Type": mime,
            "Content-Disposition": f'attachment; filename="{safe}"',
        }
        return self._req("POST", "/media", raw=content, extra_headers=headers)

    def create_post(self, post_type, title, content, status="publish",
                    featured_media=None):
        body = {"title": title, "content": content, "status": status}
        if featured_media:
            body["featured_media"] = featured_media
        return self._req("POST", f"/{post_type}", json_body=body)

    def next_nes_photos_number(self):
        """既存の "NES PHOTOS NNN" 投稿の最大番号 + 1 を返す。"""
        maxnum = 0
        page = 1
        while True:
            try:
                items = self._req("GET", f"/posts?per_page=100&page={page}&_fields=title")
            except urllib.error.HTTPError as e:
                if e.code == 400:  # ページ超過
                    break
                raise
            if not items:
                break
            for it in items:
                t = (it.get("title", {}) or {}).get("rendered", "")
                m = re.search(r"NES\s*PHOTOS\s*0*(\d+)", t, re.I)
                if m:
                    maxnum = max(maxnum, int(m.group(1)))
            if len(items) < 100:
                break
            page += 1
        return maxnum + 1


# ============================================================
# 投稿処理
# ============================================================

def build_files_map(room_id, token):
    """message_id -> 画像ファイル情報(dict) のマップを作る。"""
    files = cw_list_files(room_id, token)
    m = {}
    for f in files:
        name = f.get("filename", "")
        if name.lower().endswith(IMAGE_EXTS):
            mid = str(f.get("message_id"))
            # 同一メッセージに複数画像があれば最初の1枚を採用
            m.setdefault(mid, f)
    return m


def get_image_bytes(room_id, file_info, token):
    url = cw_file_download_url(room_id, file_info["file_id"], token)
    if not url:
        raise RuntimeError("ダウンロードURLを取得できませんでした")
    return file_info.get("filename", "photo.jpg"), http_download(url)


PHOTO_GUIDE = (
    "⚠️ 写真の取り込みに失敗しました。下記の形式で「写真1枚」と一緒に投稿してください。\n"
    "[info][title]NES PHOTOS テンプレ[/title]"
    "魚種: RAINBOW TROUT\n"
    "場所: NORTHERN HOKKAIDO\n"
    "ロッド: NES 480-4 8'0\" #4/5\n"
    "ライン: NES-FLAT MAGIC SHOOTING LINE 35lb proto[/info]\n"
    "※ラベル無しで「魚種・場所・ロッド・ライン」を4行で書いてもOKです。"
)

NEWS_GUIDE = (
    "⚠️ 投稿の取り込みに失敗しました。下記の形式で投稿してください（写真は任意）。\n"
    "[info][title]NEWS テンプレ[/title]"
    "タイトル: 公式LINEを開設しました\n"
    "本文: 友だち登録はこちらから。最新情報をお届けします。[/info]\n"
    "※ラベル無しで「1行目=タイトル、2行目以降=本文」でもOKです。"
)


def process_photo(msg, files_map, wp, ch, mention, room_id, token, dry):
    mid = str(msg["message_id"])
    sender = (msg.get("account") or {}).get("name", "?")
    parsed = parse_photo(msg.get("body", ""))
    file_info = files_map.get(mid)

    if parsed is None or file_info is None:
        reason = []
        if parsed is None:
            reason.append("テンプレ未検出")
        if file_info is None:
            reason.append("画像なし")
        log(f"  [photos] msg {mid} ({sender}) スキップ: {' / '.join(reason)}")
        if not dry:
            cw_post_message(room_id, PHOTO_GUIDE, token)
        return False

    species = parsed["species"]
    body_lines = [parsed["species"], parsed["location"], parsed["rod"], parsed["line"]]
    body_lines += parsed.get("extras", [])
    body_lines = [b for b in body_lines if b]

    if dry:
        log(f"  [photos] msg {mid} ({sender}) → 投稿予定:")
        log(f"          タイトル: NES PHOTOS NNN – {species}")
        for b in body_lines:
            log(f"          本文: {b}")
        log(f"          画像: {file_info.get('filename')}")
        return True

    filename, content = get_image_bytes(room_id, file_info, token)
    media = wp.upload_media(filename, content)
    media_id = media.get("id")

    num = wp.next_nes_photos_number()
    title = f"NES PHOTOS {num:03d} – {species}"
    html = "\n".join(f"<p>{html_escape(b)}</p>" for b in body_lines)
    res = wp.create_post(ch["post_type"], title, html,
                         status=ch.get("status", "publish"),
                         featured_media=media_id)
    link = res.get("link", "")
    log(f"  [photos] 公開: {title} ({link})")
    cw_post_message(room_id, f"{mention}✅ HPに公開しました\n{title}\n{link}", token)
    return True


def process_news(msg, files_map, wp, ch, mention, room_id, token, dry):
    mid = str(msg["message_id"])
    sender = (msg.get("account") or {}).get("name", "?")
    parsed = parse_news(msg.get("body", ""))

    if parsed is None:
        log(f"  [news] msg {mid} ({sender}) スキップ: テンプレ未検出")
        if not dry:
            cw_post_message(room_id, NEWS_GUIDE, token)
        return False

    title = parsed["title"]
    paras = [p for p in parsed["body"].split("\n") if p.strip()]
    if parsed.get("link"):
        paras.append(f'<a href="{html_escape(parsed["link"])}" target="_blank" '
                     f'rel="noopener">{html_escape(parsed["link"])}</a>')
    html = "\n".join(f"<p>{html_escape(p)}</p>" if not p.startswith("<a ")
                     else f"<p>{p}</p>" for p in paras)
    file_info = files_map.get(mid)

    if dry:
        log(f"  [news] msg {mid} ({sender}) → 投稿予定:")
        log(f"          タイトル: {title}")
        for p in paras:
            log(f"          本文: {p}")
        if file_info:
            log(f"          画像: {file_info.get('filename')}")
        return True

    media_id = None
    if file_info:
        filename, content = get_image_bytes(room_id, file_info, token)
        media = wp.upload_media(filename, content)
        media_id = media.get("id")

    res = wp.create_post(ch["post_type"], title, html,
                         status=ch.get("status", "publish"),
                         featured_media=media_id)
    link = res.get("link", "")
    log(f"  [news] 公開: {title} ({link})")
    cw_post_message(room_id, f"{mention}✅ HPに公開しました\n{title}\n{link}", token)
    return True


PROCESSORS = {"photos": process_photo, "news": process_news}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="WordPressに書き込まず解析結果のみ表示")
    ap.add_argument("--init", action="store_true",
                    help="既存メッセージを処理済みにして初期化（投稿しない）")
    ap.add_argument("--channel", choices=["photos", "news"],
                    help="片方の部屋だけ処理")
    ap.add_argument("--force-all", action="store_true",
                    help="未処理判定を無視して全メッセージ再処理（テスト用）")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH)
    if not cfg:
        log("config.json が読めません。終了します。")
        sys.exit(1)
    cfg_local = load_json(LOCAL_CONFIG_PATH, {})

    token = get_secret(cfg_local, "CHATWORK_TOKEN")
    wp_user = get_secret(cfg_local, "WP_USER")
    wp_pass = get_secret(cfg_local, "WP_APP_PASSWORD")
    if not token:
        log("CHATWORK_TOKEN が未設定です。終了します。")
        sys.exit(1)

    wp = None
    if not args.dry_run and not args.init:
        if not (wp_user and wp_pass):
            log("WP_USER / WP_APP_PASSWORD が未設定です。終了します。")
            sys.exit(1)
        wp = WP(cfg["wp_base_url"], wp_user, wp_pass)

    mention = ""
    if cfg.get("notify_mention_id"):
        mention = f"[To:{cfg['notify_mention_id']}]\n"

    state = load_json(STATE_PATH, {})
    channels = cfg["channels"]

    for key, ch in channels.items():
        if args.channel and key != args.channel:
            continue
        room_id = str(ch["room_id"])
        log(f"=== 部屋 {key} (room {room_id}) ===")
        try:
            msgs = cw_get_messages(room_id, token, force=True)
        except urllib.error.HTTPError as e:
            log(f"  メッセージ取得失敗 HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}")
            continue
        # 古い順に処理
        msgs.sort(key=lambda m: (int(m.get("send_time", 0)), str(m.get("message_id"))))

        room_state = state.setdefault(room_id, {"processed": [], "initialized": False})
        processed = set(str(x) for x in room_state.get("processed", []))

        # 初回 or --init はサイレント記録
        if args.init or (not room_state.get("initialized") and not args.force_all):
            for m in msgs:
                processed.add(str(m["message_id"]))
            room_state["processed"] = list(processed)[-MAX_PROCESSED_KEEP:]
            room_state["initialized"] = True
            log(f"  初期化: {len(msgs)}件を処理済みに記録（投稿なし）")
            continue

        files_map = build_files_map(room_id, token)
        proc = PROCESSORS[key]
        n_done = 0
        for m in msgs:
            mid = str(m["message_id"])
            if mid in processed and not args.force_all:
                continue
            try:
                ok = proc(m, files_map, wp, ch, mention, room_id, token, args.dry_run)
                if ok:
                    n_done += 1
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                log(f"  msg {mid} 失敗 HTTP {e.code}: {detail}")
            except Exception as e:
                log(f"  msg {mid} 失敗: {e}")
            finally:
                if not args.dry_run:
                    processed.add(mid)

        if not args.dry_run:
            room_state["processed"] = list(processed)[-MAX_PROCESSED_KEEP:]
        log(f"  処理 {n_done}件")

    if not args.dry_run:
        save_json(STATE_PATH, state)
    log("完了")


if __name__ == "__main__":
    main()
