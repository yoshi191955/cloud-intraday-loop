# cloud-intraday-loop — クラウド毎時ループ（PCオフでも稼働）

GitHub Actions（無料のクラウドcron）で、**あなたのPCの状態と無関係に**、
米国市場時間中の毎時、機械的モメンタムで米国株を予測→2時間後に採点→
コスト後(往復0.5%+利益25%課税)の実績を蓄積します。市場データは yfinance（無料・キー不要）。
**秘密情報は一切不要**。ログはリポジトリにコミットされ、GitHub Pages のダッシュボードで可視化。

> ペーパー（名目）検証のみ。実弾（実際の発注）は行いません。投資助言ではありません。

## なぜ 2時間 horizon か
15分足の平均値幅（≈0.32%）は往復コスト0.5%に届かず、方向が当たっても構造的にコスト負け。
2時間なら想定値幅≈0.9%でコストを超え、黒字が残り得る（あなたの既存ログで試算済み）。
だから「予測は毎時・採点は2時間保有」に設計。ダッシュボードが net-after-cost で実態を示します。

## セットアップ（初回のみ・5〜10分・無料）
1. GitHub でアカウント作成 → **新規リポジトリ**を作成（Private可）。
2. このフォルダの中身を丸ごとアップロード（`Add file → Upload files` にドラッグ）。
   構成:
   ```
   .github/workflows/intraday-loop.yml
   intraday_loop.py
   watchlist.csv
   requirements.txt
   docs/index.html
   docs/predictions_intraday.csv   (最初は空・ヘッダのみ)
   ```
3. リポジトリの **Settings → Actions → General → Workflow permissions** を
   **「Read and write permissions」** に設定（ログのコミットに必要）。
4. **Settings → Pages** で **Source: Deploy from a branch**、Branch: `main` / フォルダ `/docs` を選択 → Save。
   数分後、`https://<ユーザー名>.github.io/<リポジトリ名>/` でダッシュボードが開きます。
5. **Actions** タブ → `intraday-loop` → `Run workflow` で手動テスト実行（市場時間外は予測せず採点のみ）。

以降は **平日の米国市場時間、毎時自動**で回ります（cron: `30 13-20 * * 1-5` UTC、
DST差はスクリプト側の市場時間判定で吸収）。

## 秘密情報について
- APIキー・パスワードは**一切不要**。yfinance は公開データ、コミットは GitHub 内蔵の権限で行います。
- したがってコード・ログ・履歴に秘密は残りません。

## カスタマイズ
- **対象銘柄**: `watchlist.csv` を編集（既定52社・相関分散）。多すぎるとyfinanceが重いので、
  最初は20〜30社程度から始めるのも可。
- **horizon / 閾値**: `intraday_loop.py` 冒頭の `HORIZON`, `THRESH`, `FAST/SLOW` を編集。
- **頻度**: `.github/workflows/intraday-loop.yml` の cron を変更（GitHubのcronは負荷で数分遅延あり）。

## 注意・既知の限界
- **機械的モメンタム**は素朴なベースライン。edge を保証しません。まずは「高頻度・2h・コスト後」で
  データを貯め、ダッシュボードの期待値/t値/後半劣化/独立セッション数で判断するのが目的。
- yfinance は稀にレート制限・欠損が出ます（その銘柄はスキップし捏造しません）。
- 同時刻の多数トレードは相関するため、t値は割り引いて解釈（ダッシュボードが警告表示）。
- 十分な独立サンプル（目安30セッション/独立100トレード）が貯まるまで、実弾判断はしない。

## ダッシュボードの見方
`docs/index.html`（＝Pages）が `predictions_intraday.csv` を読み、
的中率・コスト後勝率・期待値・t値・独立セッション数・後半劣化アラート・銘柄別ビュー・
5社候補の妥当性（多重比較を避ける最低8トレード条件）・近似エクイティ曲線を表示します。
