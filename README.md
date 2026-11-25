# rss-feeds

RSS の無いサイトの新着一覧を RSS 2.0 に変換し、GitHub Pages で公開する個人的な仕組みです。

- 1時間ごとに GitHub Actions が一覧ページを取得し、`feeds/` の XML を更新します。
- 取得に失敗したサイトは前回の XML をそのまま残すので、フィードが空になることはありません。
- サイトの追加は `sites.json` に定義を足し、必要なら `generate.py` に対応パーサーを追加します。

## フィード

- UOMO 新着記事: `feeds/uomo.xml`
