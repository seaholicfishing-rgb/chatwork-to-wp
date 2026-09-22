# -*- coding: utf-8 -*-
"""短命トークン(ig_short_token.json)を長期トークン(60日)に交換して確認するローカル用ツール。
結果は ig_long_token.json（gitignore対象・ローカルのみ）へ。config.localのig_access_tokenも更新する。"""
import json
import urllib.parse
import urllib.request

cfg = json.load(open("config.local.json", encoding="utf-8"))
short = json.load(open("ig_short_token.json"))["access_token"]

q = urllib.parse.urlencode({
    "grant_type": "ig_exchange_token",
    "client_secret": cfg["IG_APP_SECRET"],
    "access_token": short,
})
d = json.loads(urllib.request.urlopen(
    "https://graph.instagram.com/access_token?" + q, timeout=60).read())
json.dump(d, open("ig_long_token.json", "w"))
print("token_type:", d.get("token_type"),
      "/ expires_in days:", round(d.get("expires_in", 0) / 86400))

cfg["ig_access_token"] = d["access_token"]
json.dump(cfg, open("config.local.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("config.local.json に ig_access_token を保存")

q2 = urllib.parse.urlencode({
    "fields": "user_id,username,account_type",
    "access_token": d["access_token"],
})
me = json.loads(urllib.request.urlopen(
    "https://graph.instagram.com/v23.0/me?" + q2, timeout=60).read())
print("me:", me)
