# ケイカ（仮称）

話すだけで支援経過。運営指導の備えは、そのついでに。

ケアマネジャーが話した内容を支援経過の文章に整え、事業所の Google ドライブにある書類と突き合わせて、確かめておきたいことを先回りで知らせるアプリです。Built with Claude: Japan 2026 の提出作品として制作中です。

> 指摘はすべて「確認してください」という形のお知らせです。法的助言や監査の代わりではなく、最終判断は事業所と保険者にあります。

## 動かし方（制作中）

Windows と Python 3.12 で確かめています。

```
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
.venv/Scripts/python.exe -m pytest
```

Claude を使う機能には API キーが要ります。アプリのフォルダに `.env` というファイルを作り、`ANTHROPIC_API_KEY=自分のキー` の1行を書いてください。`.env` はリポジトリに入りません。

書類の読み取りを試すには、次のように実行します（架空データの基準日は 2026-11-04 です）。

```
set KEIKA_AS_OF=2026-11-04
.venv/Scripts/python.exe -m keika.reading --user U04
```

## データについて

`sample_data/` と `eval/answer_key.csv` はキックオフ前に作った架空の入力データです。実在の人物・事業所とは関係ありません。詳しくは [sample_data/README.md](sample_data/README.md) を見てください。

## 作る過程の記録

[docs/claude-usage.md](docs/claude-usage.md) に、Claude をどこでどう使ったかと、試行錯誤を残しています。
