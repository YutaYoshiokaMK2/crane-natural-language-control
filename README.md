# CRANE+ V2の自然言語制御

フィジカルAI講座の最終課題として，自然言語の指示からGazebo内のCRANE+ V2を操作するシステムを実装した．

LLMは現在の関節状態を参照し，目標角度，動作時間，操作順序を計画する．実行側では指示の範囲と数値制限を検査し，MCP・ROS 2を介して関節を制御する．固定のジェスチャーを選択する方式ではなく，関節単位の計画生成を対象とする．

## 必要環境

Windows側でLLMとMCPクライアントを動かし，Dockerコンテナ内でROS 2・Gazebo・MCPサーバーを動かす構成である．以下は使用・確認した環境であり，すべての項目を同じバージョンへ固定しているわけではない．

### ホスト側

| 項目 | 使用・確認した環境 |
| --- | --- |
| OS・シェル | Windows / PowerShell |
| コンテナ実行環境 | Docker Desktop（WSL 2バックエンド） |
| Python実行環境 | `uv` 0.13.0，Python 3.14.6（uv管理） |
| ローカル推論 | Ollama（検証時0.40.11） |
| LLM | `qwen3:4b`，思考なし |

Ollamaは[公式配布](https://github.com/ollama/ollama/releases)の `ollama-windows-amd64.zip` を `.tmp/ollama/` に展開する．実行ファイルの配置先は `.tmp/ollama/ollama.exe`．

検証に使用したPCはIntel Core Ultra 7 258V，メモリ32 GBである．GPU加速は未検証であり，この構成を最低ハードウェア要件とするものではない．

### コンテナ側

| 項目 | 使用環境 |
| --- | --- |
| コンテナ名 | `pai_ros2` |
| Dockerイメージ | `airobotbook/ros2-desktop-ai-robot-book-humble:latest` |
| ROS | ROS 2 Humble |
| Python | 3.10.12 |
| シミュレーター | 講義用コンテナに同梱されたGazebo |
| ROSワークスペース | `/home/ubuntu/airobot_ws`（ビルド済み） |
| DDS実装 | Cyclone DDS（`rmw_cyclonedds_cpp`） |

ROS側では以下のパッケージを使用する．講義用ワークスペースとその依存パッケージが導入済みであることを前提とし，本リポジトリではROS環境を一から構築しない．

| パッケージ | 用途 |
| --- | --- |
| `rclpy` | ROS 2のPythonクライアント |
| `sensor_msgs` / `std_msgs` / `trajectory_msgs` | 関節状態，ロボット記述，軌道指令のメッセージ |
| `rmw_cyclonedds_cpp` | ROS 2通信 |
| `crane_plus_gazebo` | CRANE+ V2のシミュレーション起動 |
| `joint_state_broadcaster` / `joint_trajectory_controller` | 関節状態の配信と軌道制御 |

GazeboおよびROSパッケージの個別バージョンは固定していない．Dockerイメージも `latest` タグを使用しており，異なるイメージ版での互換性は未検証である．

### Pythonパッケージ

| パッケージ | バージョン・導入方法 |
| --- | --- |
| `mcp` | `1.26.0` に固定．`requirements.txt` と実行コマンドで指定 |
| `jsonschema` | MCP SDKの依存として `4.20.0` 以上を導入．確認環境では `4.26.0`，個別固定なし |

MCP SDK 1.26.0のPython要件は3.10以上である．ホスト側は `uv run --with mcp==1.26.0`，コンテナ側は `prepare.ps1` により追加パッケージを導入する．ROS用Pythonパッケージはコンテナの既存環境を利用する．

## システム構成

```text
自然言語の指示
  ↓
Ollama / Qwen3 4B
  指示の分類 → 操作範囲の抽出 → 原文との照合 → 角度・時間の計画
  ↓
Pythonクライアント
  計画の検査・絶対目標への変換
  ↓ MCP（stdio）
専用MCPサーバー
  ↓ ROS 2 Humble
Gazebo / CRANE+ V2
  関節移動・到達確認
```

推論にはローカルのQwen3 4Bを思考なしで使用する．追加学習やクラウドAPIは使用しない．ROS 2とGazeboは講義用Dockerコンテナを利用し，本リポジトリのコードと追加依存はコンテナ内の専用ディレクトリへ配置する．

MCPサーバーの公開ツールは以下の3種類．

| ツール | 処理 |
| --- | --- |
| `get_joint_state` | 現在の関節角度とURDFの制限値の取得 |
| `move_joints` | アームの関節への絶対角度指令と到達確認 |
| `set_gripper` | グリッパーへの絶対角度指令と到達確認 |

## 計画生成と実行前検査

LLMの出力をそのままROSへ送信せず，操作範囲と角度計画を分けて扱う．

1. 対象関節，手順，指定時間，復帰・再実行の有無の抽出
2. 別の推論による対象関節・保持関節・順序と同時性・基準姿勢・指定パラメータの照合
3. 操作範囲の固定と，その範囲内での角度・未指定時間の計画
4. 計画全体の検査と，実行直前の関節状態の再取得
5. MCPツールの順次呼び出しと各操作の到達確認

操作範囲はJSON SchemaとPython側の検証で制約する．各手順の根拠となる引用文は，原文から切り出した候補に限定する．離れた箇所の継ぎ合わせや言い換えを防ぎ，引用が原文の一部と完全一致することを検査する．関節や手順の追加・欠落，順序の変更，指定時間の変更を検出した場合は実行しない．解釈・照合に失敗した場合も停止する．角度計画は初回を含めて最大3回試行し，その間も操作範囲は変更しない．

絶対角度の指定は `set_absolute` と `absolute_targets` に保持し，指定値を直接目標とする．例えば「0 radにする」という指示をLLMに相対移動量へ変換させない．相対移動は `offsets` で表し，指示開始時の関節角度を基準に絶対目標へ変換する．

既定の動作は自動実行．任意の `--confirm-plan` オプションを指定した場合に限り，計画表示後の確認入力を必要とする．

## 起動と操作

以下のコマンドは，リポジトリのルートを作業ディレクトリとしてPowerShellで実行する．

### 1. シミュレーターの起動

Docker Desktopの起動後，コンテナへコードと依存パッケージを配置し，Gazeboを起動する．

```powershell
docker start pai_ros2
powershell -NoProfile -ExecutionPolicy Bypass -File .\prepare.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\launch-gazebo.ps1
```

`prepare.ps1` の配置先は `/tmp/crane-nlc` と `/tmp/crane-final-deps` であり，講義用ワークスペースのソースは変更しない．`ExecutionPolicy Bypass` は起動したPowerShellプロセスにのみ適用する．

Gazeboの画面は `http://localhost:6080` の講義デスクトップで確認する．VNCの認証は講義環境の設定に従う．起動ログはコンテナ内の `/tmp/crane-final-gazebo.log` に出力する．

起動スクリプトは既存のCRANE+ launchを検出した場合，重複起動を省略する．同一ROS domainでの複数クライアントによる同時制御は想定していない．

### 2. ローカルLLMの起動

Ollamaサーバーが停止している場合に実行する．

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-local-llm.ps1
```

モデルの取得は初回のみ必要．

```powershell
.\.tmp\ollama\ollama.exe pull qwen3:4b
```

サーバーは `127.0.0.1:11434` で待ち受け，`OLLAMA_NO_CLOUD=1` を設定する．モデルの保存先は `.tmp/models` である．起動スクリプトは既存サーバーの有無を判定しないため，重複して実行しない．

### 3. 自然言語による操作

会話形式での起動

```powershell
uv run --with mcp==1.26.0 python local_client.py --docker --execute
```

指示はPowerShellのプロンプトではなく，起動後の `instruction (exit to quit)>` に入力する．英語の指示を中心に検証している．

肩関節だけを大きく曲げる例

```text
Please bend only the shoulder joint significantly. Ensure the arm tilts noticeably—do not just make a small movement. Do not change the angles of any other joints.
```

肩の姿勢を保ち，ベース関節と肘関節を同時に大きく動かす例

```text
Please move only the base joint and the elbow joint significantly and simultaneously. Ensure that the movements are substantial enough to result in a clearly noticeable change in posture, rather than just small adjustments. Do not alter the angles of the shoulder joint, the wrist joint, or the gripper.
```

アーム4関節を0 radに設定する例

```text
Set the base, shoulder, elbow, and wrist joints to 0 radians over 4 seconds. Keep the gripper unchanged.
```

単発の指示は `--prompt` で指定する．`--execute` を省略した場合は，関節状態の取得と計画表示までを行い，移動指令は送信しない．

```powershell
uv run --with mcp==1.26.0 python local_client.py --docker --prompt "From your current posture, move only the wrist over 3 seconds. Keep all other joints still."
```

### 4. 終了と再開

クライアントでは `exit` を入力する．PowerShellへ戻った後，コンテナを停止し，LLMのモデルをメモリから解放する．

```powershell
docker stop pai_ros2
.\.tmp\ollama\ollama.exe stop qwen3:4b
```

Ollamaサーバー自体を停止する場合は，起動した `.tmp/ollama/ollama.exe` のプロセスを終了する．Docker Desktopは通知領域のメニューからQuitを選んで終了する．

再開時は起動手順を繰り返す．コンテナ再起動後のGazeboは新しい初期状態となるため，通常は `--resume` を付けずに開始する．

`--resume` はローカルの実行ログから元姿勢と前回動作を復元するオプションである．元姿勢とは，その会話で最初の指示を受け付けた時点の関節角度を指す．拒否・未完了・プレビューの記録，および現在の契約バージョンで検証されていない動作は再実行対象としない．

## 検証

### 自動テスト

関節の許可範囲，手順・時間の制約，絶対角度の保持，再実行条件，拒否時の指令抑止などを検査する．単体・結合テスト34件の成功を確認した．

```powershell
uv run --with mcp==1.26.0 python -m unittest test_instruction_contract test_plans test_client_recovery
```

### 関節制御の確認

MCP経由の関節状態取得（LLMによる推論・移動指令なし）

```powershell
uv run --with mcp==1.26.0 python mcp_smoke.py --docker --state-only
```

各関節を順に0.05 rad動かして元へ戻す試験

```powershell
uv run --with mcp==1.26.0 python mcp_smoke.py --docker --hold 2
```

### LLMを含む検証

Gazebo上での自動実行の確認結果

| 指示 | 結果 |
| --- | --- |
| 数値を指定せず，肩関節だけを大きく曲げる | LLMが約0.30 rad，2秒を選択し，到達成功．他4関節を維持 |
| 続けてベースと肘を同時に大きく動かす | LLMが両関節とも約0.30 rad，2秒を選択し，1ステップで到達成功．肩を約0.30 radに保ち，手首・グリッパーも維持 |
| 現在姿勢から手首だけを3秒で動かす | 0.150→目標0.200 radで到達成功．他4関節の変動は最大約1e-10 rad |
| アーム4関節を4秒で0 radに設定する | 全4関節で到達成功．静止後の最大絶対角度は約1.18e-12 rad．グリッパーは約0.1 radで維持 |

全関節・グリッパーの往復，タイムアウト時の停止もMCP経由で確認済みである．一方，複雑な追加指示や再実行の倍率変更は，現在の実装で十分な実行検証ができていない．

指示の解釈・照合・計画生成だけを評価するスクリプトも用意した．以下はMCPへ接続せず，ロボットを動かさない．

```powershell
uv run --with mcp==1.26.0 python evaluate_contracts.py --plans
```

これは評価用コマンドであり，全ケースの成功を示すものではない．実モデルでは関節の取り違え，不要な復帰，正しい指示の過剰な拒否が発生している．

## 制約と未対応事項

| 項目 | 設定 |
| --- | --- |
| アームの目標角度 | ±0.4 rad以内 |
| グリッパーの目標角度 | ±0.15 rad以内 |
| 1操作あたりの関節角度差 | 0.4 rad以下 |
| 動作時間 | 1〜15秒 |
| 計画の最大ステップ数 | 8 |
| 到達判定 | 新しい状態5サンプルで誤差0.005 rad以内 |
| タイムアウト | 壁時計で `duration * 4 + 5` 秒 |

上記のデモ用制限に加え，Gazebo用URDFの可動範囲も検査する．関節名で状態を対応づけ，配列順には依存しない．計画中に関節状態が0.01 radを超えて変化した場合は実行を中止する．

アームとグリッパーは別々に操作し，省略した関節は指令直前の角度を維持する．両者の同時動作には対応していない．明示された全姿勢復帰はコントローラーごとに分割するが，合計時間の指定がある場合は自動分割せず停止する．

相対移動では，現在姿勢から0.02 rad以上の変位を計画に必要とする．絶対角度の指定では，すでに目標にいる関節の保持を認める．「半分の時間」などの再実行で動作時間が1秒未満になる場合は，時間を自動変更せず停止する．

逆運動学，画像認識，物体把持，障害物回避，自己衝突・机との接触検査は対象外である．許容範囲内の全姿勢について衝突しないことは検証していない．グリッパーの開閉方向の視覚的な校正も未完了である．タイムアウト時は後続操作を停止するが，送信済み軌道のキャンセルは未実装である．

LLMによる解釈と照合が同じ誤りを起こす可能性は残る．構造・数値の検査は，自然言語の意味を完全に保証するものではない．「目で分かる程度」という指示で0.05 radの小さな移動が選ばれた例もあり，定性的な大小の指定が常に同じ角度になるわけではない．また，複数回のローカル推論を行うため，指示の受付から実行開始までに時間を要する．日本語や自由な追加指示の安定性は未確認である．

## ファイル構成

| ファイル | 役割 |
| --- | --- |
| `local_client.py` | Ollamaへの問い合わせ，計画検査，MCP呼び出し |
| `instruction_contract.py` | 操作範囲の定義，生成スキーマ，指示との整合性検査 |
| `mcp_server.py` / `mcp_connection.py` | MCPツールとstdio接続 |
| `crane_control.py` / `motion_policy.py` | ROS 2制御，到達確認，デモ用制限 |
| `prepare.ps1` / `launch-gazebo.ps1` / `start-local-llm.ps1` | 実行環境の準備と起動 |
| `mcp_smoke.py` / `verify_timeout.py` | 関節制御とタイムアウトの検証 |
| `evaluate_contracts.py` / `evaluate_local.py` | LLMを含む評価 |
| `test_*.py` | 単体・結合テスト |
| `visible_demo.py` / `focus_camera.py` | 動作の視認用試験とカメラ調整 |

モデル，実行ログ，生成物はGit管理外とする．モデルは `.tmp/models`，指示の解釈・計画・実行結果は `artifacts/` に保存する．

## 参考資料

- [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/v1/)
- [Ollama Chat API](https://docs.ollama.com/api/chat)
- [Qwen3 4B](https://ollama.com/library/qwen3:4b)
- [LLMとROS2で亀を動かす！（Qiita）](https://qiita.com/t13801206/items/f27b0f11aed92c1db890)
- [ROS MCP Server](https://github.com/robotmcp/ros-mcp-server)
