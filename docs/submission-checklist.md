# 提出前の確認

提出の操作の前に、毎回この順で確かめます。

1. `python tools/check_public.py <禁止語のファイル>` が「問題なし」を返す。API キーの形、会話ログや `.env` や `data/` のファイル、000- 以外の電話番号、禁止語（実在の人名や事業所名。ファイルはリポの外に置く）を、git で追跡中の全ファイルから探します。
2. `git log --all --stat` の履歴にも、上の物が入っていない（消したファイルも履歴に残るため、`git log --all -p -S"sk-ant-"` と禁止語でも探す）。
3. 会話ログ（Claude Code の jsonl など）、作戦メモ、実データをリポに入れていない。`docs/claude-usage.md` は要約で、ログの貼り付けではない。
4. 映像と README に、実データ、ナーシングの画面、実在の人名が映っていない。実地で使った話をしていない。
5. 画面と README と映像で、AI が下書きと指摘を出し、最終判断は人が行うと分かる。病気が分かる、診断できる、といった言い方や、特定の薬や商品をすすめる言い方をしていない。
6. 薬名は自動で直らず、候補はその利用者の書類か確定済みの記録にある名前だけで、「〇〇のことですね」のような推測の言い方が無い。
7. `pytest` が全部通り、`python eval/run_boundary.py --db <架空データの DB>` の「線を越えた数」が 0。
8. MCP の鍵が無い（`ls data/*.mcp_on` が「無い」と返す）。鍵が無い時に `record_text` が断ることを試験で確かめている。
9. 実データの混入を調べる: 禁止語のファイルに実在の利用者名と事業所名を入れて `tools/check_public.py` を通し、映像に使う DB は `demo/snapshot/keika.db` から作り直した物だけを使う（`review_items`、`records`、`drafts`、`intake_originals` に `mcp` 由来の行が 0）。
10. `data/quarantine/` が提出物に入らない: `git ls-files data` が空で、`git status --ignored` で `data/` が対象外のまま。隔離したファイルは開かない。
11. 作る間はリポを非公開にしている。上がすべて通ったら公開に戻し、ログインしていないブラウザでリポと `LICENSE` が開けることを確かめてから提出する。公開に戻さないと提出の条件を満たさない。
12. `python tools/desk_check.py` の「NG」が 0 件（本物の Claude を呼ぶ。約 $0.3）。答えも読む。README に「ケイカの答えの全文は機械で保証できない」と書いてある。
13. `git diff keep-f64358a -- eval src/keika/claude_checks.py src/keika/intake.py` が空（点検、整文、測定の経路を変えていない）。
14. このアプリからメールが出ない（`smtplib` などが無く、送信の記録は `send_mail` の1か所だけであることを `tests/test_outbox.py` で確かめている）。人が〔送信〕を押した時だけ控えに残る。ケイカの答えが自動で記録に入らない。
15. 試用の複製（`data/*_try.db`、`data/trial_base.db`、`data/desk_dev.db`）が提出物に入らない。`data/` は対象外のまま。
