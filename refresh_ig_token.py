# -*- coding: utf-8 -*-
"""Instagram長期トークン(60日)の自動リフレッシュ（ローカル定期タスク用・月1回想定）。

1. config.local.json の ig_access_token を refresh_access_token エンドポイントで更新
   （発行/前回リフレッシュから24時間経過後〜失効前まで何度でも可）
2. 新トークンを config.local.json と ig_long_token.json へ保存（どちらもgitignore対象）
3. gh CLI で GitHub Secrets の IG_ACCESS_TOKEN を更新
4. 結果をChatworkインスタグラム通知部屋へ投稿（トークン本体は載せない）

使い方:  python refresh_ig_token.py [--no-notify]
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = "seaholicfishing-rgb/chatwork-to-wp"
NOTIFY_ROOM = "440514595"   # インスタグラム通知部屋
MENTION = "[To:2238339]"    # sohei0801
LOG_PATH = os.path.join(BASE, "refresh_log.txt")


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(cfg, body):
    if "--no-notify" in sys.argv:
        return
    try:
        data = urllib.parse.urlencode({"body": body}).encode()
        req = urllib.request.Request(
            f"https://api.chatwork.com/v2/rooms/{NOTIFY_ROOM}/messages",
            data=data, headers={"X-ChatWorkToken": cfg["CHATWORK_TOKEN"]})
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        log(f"Chatwork通知失敗: {e}")


def find_gh():
    gh = shutil.which("gh")
    if gh:
        return gh
    for p in (r"C:\Program Files\GitHub CLI\gh.exe",
              r"C:\Program Files (x86)\GitHub CLI\gh.exe"):
        if os.path.exists(p):
            return p
    raise RuntimeError("gh CLI が見つかりません")


def main():
    cfg_path = os.path.join(BASE, "config.local.json")
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    old_token = cfg["ig_access_token"]

    # 1. リフレッシュ（新しい60日トークンを取得）
    q = urllib.parse.urlencode({
        "grant_type": "ig_refresh_token",
        "access_token": old_token,
    })
    try:
        d = json.loads(urllib.request.urlopen(
            "https://graph.instagram.com/refresh_access_token?" + q,
            timeout=60).read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        log(f"リフレッシュ失敗 HTTP{e.code}: {detail}")
        notify(cfg, f"{MENTION}[warning]IGトークンのリフレッシュに失敗しました。"
                    f"\nHTTP {e.code}: {detail[:300]}"
                    f"\nrefresh_ig_token.py を確認してください。"
                    f"（失効するとストーリーズ自動投稿が止まります）")
        sys.exit(1)

    new_token = d["access_token"]
    expires_in = d.get("expires_in", 0)
    expire_date = (datetime.date.today()
                   + datetime.timedelta(seconds=expires_in))
    log(f"新トークン取得OK（残り{round(expires_in / 86400)}日 → {expire_date}まで）")

    # 2. ローカル保存
    d["refreshed_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    json.dump(d, open(os.path.join(BASE, "ig_long_token.json"), "w"))
    cfg["ig_access_token"] = new_token
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    log("config.local.json / ig_long_token.json 更新")

    # 3. GitHub Secrets 更新（トークンはstdin渡し＝コマンドラインに出さない）
    r = subprocess.run(
        [find_gh(), "secret", "set", "IG_ACCESS_TOKEN", "--repo", REPO],
        input=new_token, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"gh secret set 失敗: {r.stderr.strip()}")
        notify(cfg, f"{MENTION}[warning]IGトークンは更新できましたが、"
                    f"GitHub Secretsへの反映に失敗しました。\n{r.stderr.strip()[:300]}"
                    f"\nローカルの config.local.json は新トークン済みです。")
        sys.exit(1)
    log("GitHub Secrets IG_ACCESS_TOKEN 更新OK")

    # 4. 完了通知
    notify(cfg, f"[info][title]IGトークン自動リフレッシュ完了[/title]"
                f"新しい60日トークンに更新しました（{expire_date}まで有効）。\n"
                f"GitHub Secrets・config.local.json とも反映済み。対応不要です。[/info]")


if __name__ == "__main__":
    main()
