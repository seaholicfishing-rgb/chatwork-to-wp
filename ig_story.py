#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NES PHOTOS 公開時に Instagram ストーリーズ画像を作って届ける。

- 釣果写真を 1080x1920 に敷き、同梱の story_banner.png（「HP 釣果報告を
  更新しました」の帯）を重ねてストーリーズ用画像を合成する。
- ig_user_id + IG_ACCESS_TOKEN が揃っていれば Instagram Graph API で
  ストーリーズへ自動投稿（画像は一旦 WordPress メディアに上げて公開URLを使う）。
- 揃っていない間は Chatwork「インスタグラム通知」部屋へ画像を送り、
  スマホで保存→手動投稿してもらう（story-repost と同じ半自動運用）。

失敗しても呼び出し元(sync.py)の HP 公開処理は止めない設計。
依存: Pillow（GitHub Actions では workflow 内で pip install する）。
"""
import io
import json
import mimetypes
import os
import time
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
BANNER_PATH = os.path.join(HERE, "story_banner.png")
CW_API = "https://api.chatwork.com/v2"

STORY_W, STORY_H = 1080, 1920
BANNER_Y = 1640  # 帯の貼り付け位置（下寄り・IG UIと重ならない高さ）


# ============================================================
# 画像合成
# ============================================================

def compose_story(photo_bytes):
    """釣果写真を1080x1920にカバーフィットし、告知帯を重ねたJPEGを返す。"""
    from PIL import Image, ImageFilter, ImageOps

    src = Image.open(io.BytesIO(photo_bytes))
    src = ImageOps.exif_transpose(src).convert("RGB")

    # 背景: 写真を全面ぼかしで敷く（縦横比が合わない部分の余白埋め）
    bg = ImageOps.fit(src, (STORY_W, STORY_H), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(28))

    # 前面: 写真を幅1080に収める（切らずに全体を見せる）
    ratio = STORY_W / src.width
    fg_h = min(int(src.height * ratio), 1500)
    fg = ImageOps.fit(src, (STORY_W, fg_h), Image.LANCZOS)
    canvas = bg
    canvas.paste(fg, (0, (STORY_H - fg_h) // 2 - 120))

    banner = Image.open(BANNER_PATH).convert("RGBA")
    canvas.paste(banner, ((STORY_W - banner.width) // 2, BANNER_Y), banner)

    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=90)
    return out.getvalue()


# ============================================================
# Instagram Graph API（完全自動ルート）
# ============================================================

def _graph_post(url, params):
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _graph_get(url, params):
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{q}", timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post_story_api(ig_cfg, access_token, image_url, log):
    """公開URLの画像をストーリーズとして投稿する。"""
    ver = ig_cfg.get("graph_version", "v23.0")
    ig_user = ig_cfg["ig_user_id"]
    base = f"https://graph.facebook.com/{ver}/{ig_user}"

    container = _graph_post(f"{base}/media", {
        "media_type": "STORIES",
        "image_url": image_url,
        "access_token": access_token,
    })
    cid = container.get("id")
    if not cid:
        raise RuntimeError(f"コンテナ作成失敗: {container}")

    # 画像は通常すぐFINISHEDになるが、念のため最大30秒待つ
    status_url = f"https://graph.facebook.com/{ver}/{cid}"
    for _ in range(6):
        st = _graph_get(status_url, {"fields": "status_code",
                                     "access_token": access_token})
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError(f"コンテナ処理エラー: {st}")
        time.sleep(5)

    pub = _graph_post(f"{base}/media_publish", {
        "creation_id": cid,
        "access_token": access_token,
    })
    if not pub.get("id"):
        raise RuntimeError(f"公開失敗: {pub}")
    log(f"  [story] Instagramストーリーズ公開 (media {pub['id']})")
    return pub["id"]


# ============================================================
# Chatwork フォールバック（半自動ルート）
# ============================================================

def post_story_chatwork(room_id, cw_token, filename, content, message, log):
    boundary = "----ChatworkBoundary" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(filename)[0] or "image/jpeg"
    lines = [
        ("--" + boundary).encode(),
        b'Content-Disposition: form-data; name="message"',
        b"",
        message.encode("utf-8"),
        ("--" + boundary).encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode("utf-8"),
        f"Content-Type: {ctype}".encode(),
        b"",
    ]
    body = (b"\r\n".join(lines) + b"\r\n" + content + b"\r\n"
            + ("--" + boundary + "--\r\n").encode())
    req = urllib.request.Request(
        f"{CW_API}/rooms/{room_id}/files", data=body, method="POST",
        headers={"X-ChatWorkToken": cw_token,
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        r.read()
    log(f"  [story] ストーリーズ用画像をChatwork部屋 {room_id} へ送信")


# ============================================================
# エントリポイント（sync.py から呼ぶ）
# ============================================================

def handle_photo_published(wp, photo_bytes, ig_cfg, cw_token, log,
                           title="", post_link=""):
    """HP公開後に呼ぶ。ストーリーズ自動投稿 or Chatworkへ画像送付。
    戻り値: Chatwork完了通知に添える一文（失敗時はその旨）。例外は投げない。
    """
    if not ig_cfg or not ig_cfg.get("enabled", True):
        return ""
    try:
        story = compose_story(photo_bytes)
        access_token = os.environ.get("IG_ACCESS_TOKEN") or ig_cfg.get("ig_access_token")

        if ig_cfg.get("ig_user_id") and access_token:
            # 完全自動: WPメディアに上げて公開URLを取得→Graph APIで投稿
            fname = f"story_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
            media = wp.upload_media(fname, story, mime="image/jpeg")
            image_url = media.get("source_url")
            if not image_url:
                raise RuntimeError("ストーリーズ画像のWPアップロード失敗")
            post_story_api(ig_cfg, access_token, image_url, log)
            return "\n📸 インスタのストーリーズにも自動投稿しました"

        room_id = ig_cfg.get("story_room_id")
        if room_id:
            msg = ("📸 ストーリーズ用画像です（" + (title or "NES PHOTOS") + "）\n"
                   "スマホで画像を保存 → Instagramのストーリーズに投稿してください")
            post_story_chatwork(room_id, cw_token,
                                f"story_{time.strftime('%Y%m%d_%H%M%S')}.jpg",
                                story, msg, log)
            return "\n📸 ストーリーズ用画像を「インスタグラム通知」部屋に送りました"
        return ""
    except Exception as e:  # ストーリーズ失敗でHP公開の流れは止めない
        log(f"  [story] 失敗（HP公開には影響なし）: {type(e).__name__}: {e}")
        return f"\n⚠️ ストーリーズ投稿は失敗しました（{type(e).__name__}）"
