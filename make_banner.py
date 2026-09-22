# -*- coding: utf-8 -*-
"""ストーリーズ用バナーPNG(story_banner.png)を生成するローカル用ツール。
GitHub Actions に日本語フォントを置かずに済むよう、文字はここで焼き込む。
Windowsの Noto Sans JP 可変フォントを使うため、実行は本人PCで:
    python make_banner.py
文言やサイズを変えたらこれを再実行してコミットする。"""
from PIL import Image, ImageDraw, ImageFont

# 控えめサイズ（2026-09-22 ユーザー指示で縮小: 目立たないさりげない帯に）
W, H = 560, 110
FONT = r"C:\Windows\Fonts\NotoSansJP-VF.ttf"

img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 角丸の半透明黒ピル
d.rounded_rectangle([0, 0, W, H], radius=28, fill=(0, 0, 0, 135))

f_main = ImageFont.truetype(FONT, 34)
f_main.set_variation_by_axes([600])
f_url = ImageFont.truetype(FONT, 21)
f_url.set_variation_by_axes([400])


def center(draw, y, text, font, fill):
    w = draw.textlength(text, font=font)
    draw.text(((W - w) / 2, y), text, font=font, fill=fill)


center(d, 16, "HP 釣果報告を更新しました", f_main, (255, 255, 255, 235))
center(d, 62, "northedge-standard.com", f_url, (195, 195, 195, 220))

img.save("story_banner.png")
print("saved story_banner.png", img.size)
