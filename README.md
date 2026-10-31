# 自然言語で操作するCRANE+ V2

「半分の時間」を繰り返す場合、直前の動作時間をさらに半分にします。グリッパーが2秒→1秒になった後、再度半分の0.5秒は下限1秒を下回るため実行できません。時間を自動変更せず、理由を表示して停止します。
時間を維持する指示例: `Return to your original posture, then repeat the previous action at the same speed.`
姿勢復帰の手順は次回の再実行対象に含めません。既存ログの `--resume` にも適用します。

フィジカルAI講座の最終課題。LLMが自然言語の指示と現在の関節状態から操作手順を考え、MCP・ROS 2を通じてGazebo内のCRANE+ V2を動かすことを目指します。

## 現在の状態

2026-10-25に講義用 `pai_ros2` を復旧。Gazebo起動、状態取得、専用MCP経由の全4関節・グリッパの往復とタイムアウトを検証しました。無料ローカルLLMからの英語指示で、肩・肘の同時移動と復帰まで実行しました。追加指示への追従には失敗例があります。

## 想定する機能

- 現在の関節状態の取得
- 関節の目標角度と動作時間を指定した移動
- グリッパの開閉
- 複数操作の組み合わせと、追加指示による変更
- 指令の検査、到達確認、実行結果の記録

追加学習は行いません。ローカルOllama＋Qwen3 4B（思考なし）を使い、指示の解釈・解釈の照合・角度計画を分離します。Pythonが許可範囲と数値を検査し、MCPツールを順次呼びます。完成済みジェスチャーのライブラリは使いません。目標角度・未指定の時間は引き続きLLMが現在の関節状態から考えます。

## 指示外の動きを防ぐ仕組み

絶対角度を指定する場合（例: `Set the base, shoulder, elbow, and wrist joints to 0 radians over 4 seconds. Keep the gripper unchanged.`）は、契約に `set_absolute` と各関節の `absolute_targets` を保持します。計画は絶対目標を直接出力し、LLMに符号付きoffsetへの変換をさせません。指定値と異なる目標は実行前に拒否します。すでに目標角度の関節は、そのまま保持できます。相対移動の指示と絶対角度の指定を区別します。

この英文でのGazebo自動実行を確認済みです。4関節の目標0 radへの到達成功後、静止状態の最大絶対角度は約1.18e-12 rad、グリッパーは約0.1 radのままでした。修正前には負の現在角度をoffsetへ変換する符号誤りがありました。実行中のクライアントは `exit` 後に再起動して修正を読み込んでください。

1. 指示から、動かしてよい関節・手順・明示された時間・復帰の有無を抽出します。各手順には指示文の引用を付け、関節名の対応づけをコードで行います。指示文に根拠のない関節や復帰は拒否します。
2. 別のLLM呼び出しで、元の指示と抽出結果を照合します。曖昧・未対応・不一致の場合は角度計画へ進まず停止します。
3. 抽出結果を固定して、関節・ステップ数・順序・基準姿勢・指定時間をJSON Schemaと実行前検証で制約します。再生成しても許可範囲を広げません。要求された移動を実質ゼロ移動で代用する計画も拒否します。
4. 実行直前に状態を再取得します。計画開始時から0.01 radを超えて姿勢が変わっていれば、計画を自動補正せず停止します。

肘だけの例外処理は削除し、全関節に同じ規則を適用しています。引用した指示文から関節名と操作の候補を絞る処理は、角度列を持つジェスチャーではありません。

**自由な自然言語の意味を完全に保証するものではありません。** 解釈と照合が同じ誤解をする可能性は残ります。既定では自動実行を維持し、不明・不一致・制約違反の場合に指令を送らず停止します。対象関節、動かさない関節、角度の変化、時間を表示します。モデルの英文説明は検証結果ではありません。

```powershell
uv run --with mcp==1.26.0 python local_client.py --docker --execute --resume
```

最新の方式では、現在姿勢から手首だけを動かす自動実行をGazeboで確認しました。手首0.150→目標0.200 rad（3秒）が到達成功、他4関節の変動は最大約1e-10 radで、姿勢復帰の指令はありませんでした。全指示の成功を保証するものではありません。Qwen2.5 3Bは新しい指示解釈で関節取り違えが多かったため、既存のQwen3 4Bへ既定値を変更しています。

必要な場合だけ `--confirm-plan` を追加すると表示後の `y` 入力を必要にできますが、既定の自動実行には確認操作を追加していません。明示的な全姿勢復帰のみ、Pythonがアームとグリッパーに分けて実行します。合計時間を指定した全姿勢復帰は、時間を勝手に倍にせず停止します。

## 実行方法（Windows＋既存講義コンテナ）

### 終了と翌日の再開

クライアントの `instruction ...>` で `exit` を入力し、PowerShellへ戻ったら `docker stop pai_ros2` でGazebo・ROS・VNCをまとめて停止します。Ollamaのモデルをメモリから解放するには `.\.tmp\ollama\ollama.exe stop qwen3:4b` を実行します。Ollamaサーバー自体も終了する場合は、タスクマネージャーで今回起動した `.tmp/ollama/ollama.exe` を終了してください。Docker Desktopも終了するなら、通知領域のメニューからQuitを選びます。

再開はDocker Desktopを起動し、本リポジトリで `docker start pai_ros2`、上記の `prepare.ps1`、`launch-gazebo.ps1` を実行します。Ollamaが終了している場合だけ `start-local-llm.ps1` を実行します（同じスクリプトを二重起動しない）。ダウンロード済みモデルを再取得する必要はありません。その後 `uv run --with mcp==1.26.0 python local_client.py --docker --execute` で開始できます。コンテナ再起動後のGazeboは新しい初期状態なので、通常は `--resume` を付けず、新しい会話として始めます。ログ・モデルはローカルに残りますが、Gitへは含めません。

前提: Docker Desktop、既存 `pai_ros2`、`uv`。PowerShellで本リポジトリへ移動します。コンテナの教材・バックアップは編集しません。新コードとMCP依存の配置先は `/tmp/crane-nlc` と `/tmp/crane-final-deps` です。

```powershell
# Docker Desktopを起動しておく
docker start pai_ros2
powershell -NoProfile -ExecutionPolicy Bypass -File .\prepare.ps1
# Gazeboがまだ動いていないときに一度だけ実行
powershell -NoProfile -ExecutionPolicy Bypass -File .\launch-gazebo.ps1
```

スクリプトは専用のPowerShellプロセスで実行します。`-ExecutionPolicy Bypass` はそのプロセスだけに適用され、PC全体やユーザーの実行ポリシーは変更しません。

画面は http://localhost:6080 の講義デスクトップで確認できます。既存VNCパスワードを使います。起動ログはコンテナ内 `/tmp/crane-final-gazebo.log`。同じROS domainで他の制御・推論ノードを同時に動かさないでください。起動スクリプトは既存のCRANE+ launchを検出すると再起動を省略し、launchなしでGazeboだけが残っている場合はエラーで停止します。

二重起動後に画面を1つ閉じても、ROSノードや別のロボットが残る場合があります。表示と制御がおかしくなった場合は、進行中の操作を終えてから `docker restart pai_ros2` を実行し、上記起動スクリプトで1組だけ起動します。VNCは再接続し、シミュレーションは初期状態になります。

LLMなしの実機能検証（Gazebo内、全関節を1つずつ0.05 rad動かして戻す）:

```powershell
docker exec -u ubuntu pai_ros2 bash -lc 'source /opt/ros/humble/setup.bash; source /home/ubuntu/airobot_ws/install/setup.bash; export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp; export PYTHONPATH=/tmp/crane-final-deps:$PYTHONPATH; python3 /tmp/crane-nlc/mcp_smoke.py'
```

このテストの移動量は0.05 rad（約2.9度）なので見た目の変化は小さめです。各姿勢を2秒保持するには末尾を `mcp_smoke.py --hold 2` にします。ターミナルの `moving 関節名 -> 目標角度` と、結果の `positions`・`success` も確認してください。

### 大きく見やすい動作

指定フォルダで以下を実行すると、肩を約20度傾けて戻し、反対側へ約20度傾けて戻します。各移動は3秒、各姿勢を3秒保持します。LLMなしの視認用試験で、全関節がほぼ直立（±0.08 rad以内）のときに実行できます。

```powershell
uv run --with mcp==1.26.0 python visible_demo.py
```

この試験はLLMに選択させるジェスチャーツールには公開していません。LLMで大きめの動きを生成する場合は、次の実行確認済み指示を使えます。

```powershell
uv run --with mcp==1.26.0 python local_client.py --docker --execute --prompt 'Make a two-step plan: first tilt only the shoulder joint with a clearly visible large angular change from its starting pose, taking 4 seconds; second return the shoulder to its initial angle, taking 4 seconds.'
```

この試行ではLLMが0.30 rad（約17度）を選び、傾きと復帰の両方で到達を確認しました。必要なら以下でGazeboのカメラを近づけられます（ロボットの姿勢は変えません）。

```powershell
docker exec -u ubuntu pai_ros2 python3 /tmp/crane-nlc/focus_camera.py
```

Ollamaは[公式Windows配布](https://github.com/ollama/ollama/releases)の `ollama-windows-amd64.zip` を `.tmp/ollama` に展開します（`ollama.exe` が直下）。本PCではこの場所へ取得しています。システムへのインストール、APIキー、課金契約は不要です。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-local-llm.ps1
.\.tmp\ollama\ollama.exe pull qwen3:4b
# 最初は計画表示のみ
uv run --with mcp==1.26.0 python local_client.py --docker --prompt 'Move the shoulder and elbow joints slightly at the same time, then return both joints to their initial angles.'
# MCPで実際に実行
uv run --with mcp==1.26.0 python local_client.py --docker --execute --prompt 'Move the shoulder and elbow joints slightly at the same time, then return both joints to their initial angles.'
# 会話形式。直前の計画を履歴として渡し、追加指示を入力できる
uv run --with mcp==1.26.0 python local_client.py --docker --execute
```

### 「元の姿勢」と前回動作の参照

元の姿勢は、その会話で最初の指示を受け付けた時点の関節角度として保存します。追加指示の開始時点の姿勢とは区別します。会話を終了して起動し直す場合、次のコマンドで `artifacts/runs.jsonl` から元の姿勢と前回の実行済み動作を復元できます。

```powershell
uv run --with mcp==1.26.0 python local_client.py --docker --execute --resume
```

`Resumed reference action:` に、再利用する前回の指示が表示されます。拒否・計画表示のみ・一部だけ完了・最大変位0.02 rad未満の記録は復元対象にしません。参照したい動作が表示されたことを確認して、`instruction ...>` に入力します。

共通制約への変更前の動作は、指示外の動作が混じる可能性があるため再実行対象にしません。その場合は元姿勢の参照のみ復元した旨を表示します。新しく動作を指示し、成功した後に「同じ動作」を使ってください。過去ログは削除・書き換えしません。

```text
Return to your original posture, then perform the same action in half the time.
```

LLMが再実行の条件を解釈し、照合に通った時間・振り幅の倍率と復帰の有無を固定します。以前の方式では、元姿勢への復帰後、同じ目標をアーム2.5秒・グリッパ1秒（元は5秒・2秒）で再実行した到達成功ログがあります。今回の共通制約への変更後は、再実行による移動は未確認です。表示だけで確認するには起動時の `--execute` を外します。

Ollamaはlocalhostに限定し、`OLLAMA_NO_CLOUD=1` を設定します。モデルは `.tmp/models`、指示解釈・照合・計画・実行記録は `artifacts/runs.jsonl`。これらはGit管理外です。新方式は複数回の推論を行うため、以前の単発計画より時間がかかります。英語を優先して試してください。日本語・自由な追加指示は安定性未確認です。

## 制御と制約

- `/robot_description` のGazebo用URDFから可動範囲を取得。関節名で対応し、配列順には依存しません。
- 現在はアーム目標を±0.4 rad、グリッパを±0.15 rad、1回の差分を0.4 rad以下、動作時間を1〜15秒に限定。URDF制限も併せて検査します。近い直立姿勢から肩を±0.35 rad動かす試験を実施しましたが、範囲内の全関節の組み合わせは未検証です。衝突回避の機能はありません。
- アームとグリッパは別々の操作。省略した関節は指令直前の現在値を維持します。
- 新しい関節状態5サンプルで、全指令関節の誤差が0.005 rad以内であることを確認。壁時計で `duration * 4 + 5` 秒を超えると失敗とし、後続操作を停止します。
- 送信成功だけでは到達成功にしません。タイムアウトは緊急停止ではなく、送信済み軌道のキャンセルは未実装です。
- 自己衝突・机との接触を検査する機能、逆運動学、画像認識、物体把持は実装しません。グリッパの開閉方向の視覚的な校正も未完了です。
- クライアントは最大8ステップの計画全体を検査し、実行時にもROS側で各指令を検査します。同時に複数クライアントを実行しないでください。
- 数値・構造の検査は、指示どおりの意味であることを保証しません。戻りの省略や「遅く」を速くする失敗もありました。まず計画表示で確認します。不正な計画は実行せず、最大2回までLLMへ修正を依頼して、その試行もログに残します。
- アームとグリッパは生成用JSON Schemaでも別グループに限定します。ゼロ指定の関節も反対グループには含めません。再計画が3回とも拒否された場合、会話モードは理由を表示して次の入力へ戻ります。`--prompt` の単発実行は終了コード1を返します。
- `offsets` は `reference` で選ぶ姿勢が基準です。`current` は今回の指示開始時、`original` は保存した元姿勢、`previous_step_1` などは前回計画の各ステップの姿勢です。ゼロは選択した基準姿勢への復帰を表します。根元の正回転は+Z軸方向で、上から見て反時計回りです。
- LLM計画には現在姿勢から0.02 rad以上の変位を必要とします。検査を通すためだけの微小動作を避けるためです。再実行で時間・振り幅を変更しても、各目標の範囲、ステップ差分、1〜15秒、最大8ステップの検査を行います。

```powershell
uv run --with mcp==1.26.0 python -m unittest test_plans.py
uv run --with mcp==1.26.0 python -m unittest test_client_recovery.py
uv run --with mcp==1.26.0 python -m unittest test_instruction_contract.py
# ローカルモデルの解釈・照合・計画を評価（MCP接続なし・ロボット移動なし）
uv run --with mcp==1.26.0 python evaluate_contracts.py --plans
# 実際のローカルLLMで英語3指示を会話履歴付きで評価（--executeで関節移動）
uv run --with mcp==1.26.0 python evaluate_local.py --execute
```

MCPはPython SDK 1.26.0のstdioを使います（講義Python 3.10を維持）。[公式SDK v1資料](https://py.sdk.modelcontextprotocol.io/v1/)、[Ollama Chat API](https://docs.ollama.com/api/chat)、[Qwen3 4Bモデル](https://ollama.com/library/qwen3:4b)。Qwen3 4Bには旧方式での追従失敗・思考モード応答未完了の記録もあります。現在は思考なし・解釈と計画の分離・共通制約の条件で再評価しています。

## 開発の引継ぎ

開発方針と既存教材の参照先は、ローカルの親フォルダにある `../HANDOFF.md` に記載しています（リポジトリ管理外）。

## 参考

- [LLMとROS2で亀を動かす！（Qiita）](https://qiita.com/t13801206/items/f27b0f11aed92c1db890)
- [ROS MCP Server](https://github.com/robotmcp/ros-mcp-server)
