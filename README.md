# chatwork-to-wp — Chatworkに投稿するだけでHP更新

専用の Chatwork 部屋に「写真＋テンプレ」を投稿するだけで、
North Edge Standard のHP（WordPress）の **NES PHOTOS** と **NEWS** が自動更新されます。

GitHub Actions が15分ごとに部屋をチェックし、新しい投稿を見つけたらWordPressに反映、
完了したら同じ部屋に「✅ 公開しました ＋ URL」を返信します。

---

## 投稿のしかた

### 📷 NES PHOTOS（部屋: 📷 NES PHOTOS 投稿用）

**写真を1枚添付**して、コメント欄に次のテンプレを書いて送信するだけ。

```
魚種: RAINBOW TROUT
場所: NORTHERN HOKKAIDO
ロッド: NES 480-4 8'0" #4/5
ライン: NES-FLAT MAGIC SHOOTING LINE 35lb proto
```

- → タイトル `NES PHOTOS 010 – RAINBOW TROUT`（番号は自動で次の数字）
- → 本文4行、アイキャッチ＝その写真 で**即公開**されます。
- ラベル（魚種: など）は無くてもOK。その場合は **上から順に「魚種・場所・ロッド・ライン」の4行**として扱います。
- 5行目以降を書くと、本文の末尾に追記されます。

### 📰 NEWS（部屋: 📰 NES NEWS 投稿用）

テンプレを書いて送信（**写真は任意**）。

```
タイトル: 公式LINEを開設しました
本文: 友だち登録はこちらから。最新情報をお届けします。
リンク: https://lin.ee/xxxxx
```

- ラベル無しなら **1行目＝タイトル、2行目以降＝本文**。
- 「リンク:」を書くと本文末尾にリンクが付きます（任意）。
- 写真を添付するとアイキャッチになります。

> うまく取り込めなかった時は、Botがその部屋に書き方ガイドを返信します。
> 直して投稿し直せばOKです（古い失敗メッセージは再処理されません）。

---

## 仕組み

```
[Chatwork 専用部屋] ──読み取り──▶ [GitHub Actions: sync.py] ──REST API──▶ [WordPress]
   写真＋テンプレ                   ・新着をmessage_idで検知              NES PHOTOS / NEWS
                                    ・テンプレ解析＋画像DL                を即時公開
                  ◀──完了通知──     ・WPに投稿＋アイキャッチ設定
```

- 重複投稿しないよう、処理済み message_id を `state.json` で管理。
- 初回（or `--init`）は既存メッセージを「処理済み」として静かに記録し、過去ログを一気に投稿しません。

---

## セットアップ（初回のみ）

### 1. WordPress アプリケーションパスワードを発行
1. WP管理画面 → **ユーザー → プロフィール**（あなたのユーザー）
2. 下の方の **「アプリケーションパスワード」** で名前に `chatwork-to-wp` と入れて **新規追加**
3. 表示された `xxxx xxxx xxxx xxxx xxxx xxxx` を控える（一度しか表示されません）

### 2. GitHubリポジトリに登録（Secrets）
リポジトリ → Settings → Secrets and variables → Actions → **New repository secret** で3つ登録:

| 名前 | 値 |
|------|----|
| `CHATWORK_TOKEN` | Chatwork APIトークン |
| `WP_USER` | WordPressのユーザー名（例: `sohei`） |
| `WP_APP_PASSWORD` | 手順1のアプリケーションパスワード |

### 3. 完了
あとは `📷 NES PHOTOS 投稿用` / `📰 NES NEWS 投稿用` の部屋に投稿するだけ。
すぐ反映したい時は GitHub の Actions タブ → `chatwork-to-wp` → **Run workflow** で手動実行できます。

---

## ローカルでテストする

`config.local.example.json` を `config.local.json` にコピーして秘密情報を記入し:

```
python sync.py --init       # まず既存メッセージを処理済みにして初期化
python sync.py --dry-run    # WPに書き込まず、解析結果だけ確認
python sync.py              # 実際に投稿
python sync.py --channel photos   # 写真の部屋だけ
python sync.py --force-all --dry-run --channel news   # 全メッセージを解析してみる（送信なし）
```

---

## 設定ファイル

- `config.json` … 部屋ID・WPのURL・投稿タイプ・公開状態（publish/draft）・通知先。**秘密情報は入れない**。
  - 「下書きで確認してから公開」に変えたい場合は該当チャンネルの `"status"` を `"draft"` にする。
- `config.local.json` … ローカル実行用の秘密情報（gitignore済み・非公開）。
- `state.json` … 処理済みメッセージの記録（自動生成・自動コミット）。

## 部屋ID（参考）

- 📷 NES PHOTOS 投稿用: `440654660`
- 📰 NES NEWS 投稿用: `440654661`
