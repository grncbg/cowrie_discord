# Cowrie → Discord 差分通知

機能・設定・状態管理・制約の詳細は [仕様書](SPECIFICATION.md) を参照してください。

Windows / Linux 共通の uv と OpenSSH クライアントで動作します。Python 3.10 以降の環境はuvで管理し、外部Pythonパッケージへの依存はありません。
1回実行すると、実行間隔を経過したcheckだけSSHで抽出・前回値との比較・通知を行って終了します。5分間隔の起動はOSに任せます。

## 動作

設定例では `cowrie-host` の `/opt/cowrie/var/log/cowrie/cowrie.json` を読み、イベントIDで選別したフィールドの集合を比較します。ホスト名とパスは環境に合わせて変更してください。

| 項目 | event_ids | json_field |
|---|---|---|
| commands | `cowrie.command.input` | `input` |
| uploads | `cowrie.session.file_download`, `cowrie.session.file_upload` | `message` |
| user | `cowrie.login.failed`, `cowrie.login.success` | `username` |
| password | `cowrie.login.failed`, `cowrie.login.success` | `password` |
| source-addresses | `cowrie.session.connect` | `src_ip` |

イベントの定義は [Cowrie公式リファレンス](https://docs.cowrie.org/en/latest/OUTPUT.html) を参照してください。`uploads` は外部からのファイル取得とSFTP/SCPアップロードを対象とし、通知には説明文を使用します。転送失敗イベントや任意のSFTP操作ログは対象に含みません。

- 初回は現在値を保存するだけです。既存分も通知したければ `notify_initial: true` にします。
- 2回目以降は新しく観測した値を `+` でDiscordに通知します。順序変更や同じ行の再出現・回数増加では通知しません。
- SSH先の `python3` でJSONログの追記分だけを取得し、Python側でイベントの選別・フィールド抽出・重複排除を行います。同じファイルは1起動につき1回取得します。
- 通知がすべて成功したチェックだけスナップショットを更新します。SSH・ログ解析・通知の失敗は終了コード1、通常終了・重複起動スキップは0です。対象イベント0件は正常な空集合、ファイル不在・権限不足・不正なJSONはエラーとして区別します。
- 通知失敗はそのcheckの実行間隔を経過した後の起動で再試行します。HTTP 429 / 5xxには最大3回の送信試行を行います（待機は1回30秒以内）。途中まで送信済みの場合や送信直後に停止した場合、再試行で通知が重複することがあります。
- 長い差分は分割し、ログに含まれる `@everyone` 等のメンションは無効化します。表示用のバッククォートを置換しますが比較する値は保持します。
- ログは `logs/monitor.log`（2 MB × 現行1本＋バックアップ3本）、状態は `state/snapshots.json` に保存します。相対パスは設定ファイルのあるディレクトリ基準です。
- 同じ状態ファイルを使う同時起動はOSのファイルロックで抑止します。状態・ロックファイルはローカルディスクに置いてください。

`uploads` はイベントの説明文の差分を検知します。バイナリ自体のハッシュ比較ではありません。
この監視では観測済みの値を累積して保持し、ローテーション後の再出現では再通知しません。削除通知は行いません。

## 手動実行

リポジトリには公開用の `config.example.json` を同梱しています。初回のみ `config.json` にコピーし、SSHホスト・監視ファイル等を編集してください。`config.json` はGit管理対象外です。既存の運用設定を上書きしないでください。

以下の `C:\path\to\cowrie-discord` と `<USER>` は例です。実際の保存先と実行ユーザーに置き換えてください。

uvとOpenSSHを利用可能にし、タスクを実行するWindowsユーザーで `ssh cowrie-host` が接続できることを確認してください。
同じユーザーの `%USERPROFILE%\.ssh\config`、秘密鍵、`known_hosts` を利用します。
対話入力を禁止し、ホスト鍵確認を必須にしているため、未登録のホスト鍵は事前に通常のSSH接続で確認・登録します。

PowerShellでプロジェクトに移動し、uvで環境を準備・実行します。初回は必要に応じてPythonがダウンロードされます。

```powershell
Set-Location 'C:\path\to\cowrie-discord'
if (-not (Test-Path -LiteralPath config.json)) { Copy-Item config.example.json config.json }
uv sync --locked
$env:DISCORD_WEBHOOK_URL = 'https://discord.com/api/webhooks/…/…'
uv run --locked cowrie_monitor.py --dry-run
uv run --locked cowrie_monitor.py
```

`--dry-run` は接続・抽出・件数比較を行い、通知も状態更新もしません（実行ログとロックファイルは作成します）。
初回の通常実行で基準値を作り、以後の変化で通知されます。別設定は `--config 'C:\path\config.json'` で指定します。

## タスクスケジューラ（5分毎）

1. **タスクの作成**で、SSH接続できるユーザーを実行ユーザーに指定します。
2. トリガーは「毎日」、繰り返し間隔「5分」、継続時間「無期限」にします。
3. 操作「プログラムの開始」を次のように設定します。
   - プログラム: `C:\Users\<USER>\.local\bin\uv.exe`（`(Get-Command uv).Source` で確認した絶対パス）
   - 引数: `run --offline --locked --project "C:\path\to\cowrie-discord" "C:\path\to\cowrie-discord\cowrie_monitor.py" --config "C:\path\to\cowrie-discord\config.json"`
   - 開始: `C:\path\to\cowrie-discord`
4. 設定の「タスクが既に実行中の場合」は「新しいインスタンスを開始しない」にします。
5. 実行ユーザーの環境変数 `DISCORD_WEBHOOK_URL` を設定します。

登録前に同じ実行ユーザーで `uv sync --locked` を一度実行してください。定期実行時の `--offline` はuvのダウンロードを禁止するもので、スクリプトのSSH・Discord通信には影響しません。

現在のPowerShellだけの `$env:...` はタスクに引き継がれません。永続化する場合は以下を実行します。

```powershell
[Environment]::SetEnvironmentVariable('DISCORD_WEBHOOK_URL', $env:DISCORD_WEBHOOK_URL, 'User')
```

環境変数がタスクのプロセスに反映されるよう、設定後にWindowsを再起動するのが確実です。
秘密のURLを引数やリポジトリに書かないでください。
「ログオンしているかどうかにかかわらず実行する」場合も、SSH鍵やエージェントがその実行コンテキストで使えることを確認します。
タスクを「実行」し、`logs/monitor.log` と「前回の実行結果」で確認できます。

### ログオン中にウィンドウを表示せず実行する

「ユーザーがログオンしているときのみ実行する」を維持し、操作を以下に変更します。パスワードの保存は不要です。

- プログラム: `C:\Windows\System32\wscript.exe`
- 引数: `//B //Nologo "C:\path\to\cowrie-discord\deploy\run_hidden.vbs" "C:\Users\<USER>\.local\bin\uv.exe"`
- 開始: `C:\path\to\cowrie-discord`

Windows Script Hostのラッパーがuvを非表示で起動し、終了を待って終了コードをタスクスケジューラへ返します。Pythonは引き続きuv経由で実行します。
サインアウト中は実行されません。画面ロック中はログオンが維持されているため実行対象です。
Windows Script Host / VBScriptが利用可能な環境向けです。

既存のタスクの起動処理だけを変更する場合は、必要に応じて管理者PowerShellから次を実行します。uvは `Get-Command uv` で検出し、必要なら `-UvExecutable` で絶対パスを指定できます。新規タスクの登録は行いません。

```powershell
.\deploy\enable_hidden_task.ps1 -TaskName cowrie
```

## 監視対象の追加

### checkごとのメンション

`mention_everyone: true` にしたcheckだけ、差分投稿の先頭に `@everyone` を付けます。省略時はfalseで、公開用設定例もすべてfalseです。必要なcheckだけ運用設定でtrueにしてください。

通常の差分投稿は全checkで継続します。長い差分の分割送信では最初の1通だけメンションします。変更なし・間隔未経過では投稿もメンションもありません。送信失敗後の再試行ではメンションも再送される場合があります。設定変更だけで比較用データや前回実行時刻はリセットされません。

check名・ログ中の `@` には表示用のゼロ幅文字を挿入し、明示的に追加した `@everyone` だけを許可します。ユーザー・ロールへのメンションは許可しません。保存・比較する文字列は変更しません。

**通知をメンションだけにするには、Discord側で対象チャンネルの通知設定を「メンションのみ」に設定してください。** またサーバーの「@everyoneと@hereを抑制」はオフにします。Webhookから個人の通知設定は変更できません。[Discordの通知設定](https://support.discord.com/hc/en-us/articles/215253258-Notifications-Settings-101)

### checkごとの実行間隔

各checkの `interval_seconds` に0以上の整数を指定します（省略時300）。**0は実行間隔によるスキップをせず、起動のたびに実行する設定**です。公開用設定例は全checkを0にしており、実行頻度をOS側の5分間隔に任せています。同時起動のロックは引き続き有効です。

OS側を5分毎、checkも300秒にすると、起動の遅延や先行checkの処理時間の違いにより、次回の判定時点で299秒程度しか経過していないことがあります。その場合は1回スキップされるため、**OS側の毎回の起動で実行したいcheckには明示的に0を指定してください**。0は連続実行や常駐を意味せず、手動起動でも1回だけ実行します。

900や3600などの正の値は、それぞれ前回の開始から15分・1時間以上経過した後の起動で実行する最小間隔です。OS側の間隔の整数倍でも、同様に1回スキップされる場合があります。

- 判定は「現在時刻 − 前回の実行開始時刻 ≥ interval_seconds」です。経過していないcheckの通知処理はスキップし、残り秒数をログに記録します。同じファイルを別のcheckが取得した場合、観測値は未到達のcheckにも保存します。
- OS側は引き続き5分毎に起動します。例えば間隔420秒なら、5分後はスキップし、通常は10分後の起動で実行します。時刻に揃える処理や、停止中の回数分の追いつき実行はしません。
- 開始時刻を `state/snapshots.json` の `last_runs` に保存します。SSH・通知失敗や実行途中の停止も実行として数え、指定間隔を待ってから再試行します。0の場合は次回起動時に再試行します。比較用の `checks` は従来どおり通知成功時にだけ更新します。
- 初回・旧形式の状態に時刻がない場合は即実行します。既存の比較用データは引き継ぐため、状態ファイルの削除は不要です。
- 間隔だけを変更すると、前回の開始時刻を基準に新しい間隔を適用します。ホスト・ファイル・抽出条件の変更や新しいcheckは即実行します。
- `--dry-run` も同じ間隔判定を行いますが、実行時刻も比較用データも更新しません。
- 保存済みの開始時刻より時計が戻った場合は即実行し、新しい時刻を基準にします。時刻はUTCのUnix秒なのでタイムゾーン変更に依存しません。

`config.json` の `checks` 配列に一意の `name` を持つ要素を追加します。例えば接続元IPを抽出する設定は次のとおりです。IPv6アドレスもそのまま取得できます。

```json
{
  "name": "source-addresses",
  "interval_seconds": 3600,
  "file": "/opt/cowrie/var/log/cowrie/cowrie.json",
  "json_field": "src_ip",
  "event_ids": ["cowrie.session.connect"]
}
```

タイムスタンプや接続IDを抽出に含めると、それらが変わるたびに新しい値と判定されます。比較したい文字列フィールドを指定してください。

### ユーザ名・パスワードの抽出

公開用設定例の `user` と `password` は、CowrieのJSON Linesログから認証成功・失敗イベントの `username` / `password` を直接取得します。ユーザ名中の `/`、空文字、前後の空白、引用符、改行も保持します。JSONのエスケープはデコードして保存します。

```json
{
  "name": "password",
  "file": "/opt/cowrie/var/log/cowrie/cowrie.json",
  "json_field": "password",
  "event_ids": ["cowrie.login.failed", "cowrie.login.success"],
  "interval_seconds": 1795
}
```

この監視ではSSH先の `python3`（3.10以降）で初回は現在ファイル全体、以後は追記分を取得します。`json_field` と空でない `event_ids` を必ず指定します。不正なJSON・UTF-8、対象イベントのフィールド欠落や文字列以外の値は取得失敗として扱い、比較用データを維持します。末尾の改行未完了行は読取り位置を進めず、完成後に次回取得します。

抽出はJSONログ専用です。旧設定の `notify_removed`、`regex`、`capture_group` は不要です。削除通知・grepによるテキスト抽出は実装していません。設定項目の一覧は[仕様書](SPECIFICATION.md#設定)を参照してください。


ファイル・抽出フィールド・対象イベントの変更時は基準値を作り直します。`notify_initial: false` なら、その時点の全候補は通知されません。

`name` を変更した場合や、SSHホスト・ファイル・抽出フィールド・イベントIDを変更した場合、そのチェックは新しい基準値を作ります。
通知設定だけの変更では基準値を保持します。状態ファイルを消すと全チェックが初回扱いになります。
壊れた状態ファイルは自動で上書きせずエラーにします。
通知の失敗はcheck単位で扱います。取得・解析が失敗すると同じファイルを参照するcheckは失敗しますが、別ファイルの処理は継続します。
SSHの独自ポート・鍵等はSSH configに記載し、必要なら `ssh.executable` に `ssh.exe` の絶対パスを指定してください。

## Linux / systemdへの移植

新規導入時はプロジェクト内で `test -e config.json || cp config.example.json config.json` を実行し、設定を編集してください。

`cowrie_monitor.py`・`incremental_reader.py`・設定・`pyproject.toml`・`uv.lock` を `/opt/cowrie-discord/` に配置し、実行ユーザーがプロジェクト内の仮想環境・ログ・状態ディレクトリを書けるようにします。
uvを導入し、実行ユーザーで `uv sync --locked --project /opt/cowrie-discord` を一度実行してください。Windowsの `.venv` はコピーせずLinuxで作り直します。
そのユーザーの `~/.ssh/config` に `cowrie-host` を設定し、対話なしのSSH接続を確認してください。
`deploy/cowrie-discord.service` の `User=monitor` は実際のユーザーに置き換えます。
`ExecStart` の `/home/monitor/.local/bin/uv` も、そのユーザーで `command -v uv` が返す絶対パスに置き換えます。
環境ファイル `/etc/cowrie-discord.env` を管理者のみ読み書き可能にし、次を記載します。

```ini
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/…/…
```

```sh
sudo chmod 600 /etc/cowrie-discord.env
sudo cp deploy/cowrie-discord.service deploy/cowrie-discord.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start cowrie-discord.service
sudo systemctl enable --now cowrie-discord.timer
journalctl -u cowrie-discord.service
```

timerは5分境界で実行し、停止中の分は起動後に1回実行します。WindowsとLinuxで同時に動かすと、それぞれから通知されるため移行時は旧タスクを停止してください。

## テスト

型チェックは必須です。mypyのstrictモードに加えて、`Any` を含む式・明示的な`Any`・未解決import由来の`Any`・デコレータ由来の`Any`を禁止しています。本体とテストを含むプロジェクト内のPythonファイルが対象です。
`Any` の導入、型チェックの無効化・除外、設定の緩和には事前の許可が必要です。
JSONは一度 `object` として受け取り、実行時に検証してから型付きデータクラスに変換します。設定はJSON抽出専用です。既存のJSON監視の状態ファイルはそのまま引き継げます。

```powershell
uv run --locked mypy --platform win32
uv run --locked mypy --platform linux
uv run --locked -m unittest discover -v
```

両OSの型チェックは現在のOS上で実行できます。Linux側の実行時動作の検証を代替するものではありません。

GitHub ActionsではWindows／LinuxとPython 3.10／3.14の組み合わせで同じチェックを行います。実ホストやWebhookの認証情報は不要です。

テストはSSHとDiscord送信をモックし、差分・再送・エラー時の状態保持・複数チェック・分割・ロックを確認します。実際のDiscordへは送信しません。

API仕様: [Discord Webhook Resource](https://discord.com/developers/docs/resources/webhook#execute-webhook)、[Allowed mentions](https://discord.com/developers/docs/resources/message#allowed-mentions-object)。


## JSONログの差分取得とローテーション

- SSH先の `python3`（3.10以降、標準ライブラリのみ）が必要です。リモートへファイルを設置せず、同梱の `incremental_reader.py` をSSH経由で実行します。
- 初回は現在ファイルを読みます。既存の比較用データは引き継ぎます。以後はデバイス・inode・バイト位置と直前256バイトのSHA-256を用いて続きだけ取得します。
- 同じファイルを参照する全JSON項目へ取得結果を振り分けます。通知間隔未到達の項目も観測値を保存し、通知は各項目の間隔に従います。
- `streams` に読取り位置、`observed` に累積観測値を一緒に永続保存してから通知します。`checks` は通知成功時に更新するため、再起動・通知失敗後も未通知の値を再送できます。部分送信後の失敗では通知が重複する場合があります。
- 同じディレクトリに元のファイル名で始まる未圧縮の退避ファイルが残っていれば、旧inodeを探し、更新日時順に未読部分を回収して新ファイルへ進みます。初回は過去の退避ファイルを遡りません。
- 旧世代が圧縮・削除済み、別ディレクトリへ移動済み、または名前の接頭辞が異なる場合は取得エラーとし、読取り位置を保持します。退避ファイルの更新日時が世代順を表す運用を前提とします。監視の停止期間より長く未圧縮の世代を残してください。
- 同じinodeのサイズ縮小や直前部分の変更を検出すると、警告して先頭から再開します。copytruncateのコピーと切詰めの間の欠落や、同一内容で再伸長した場合の検出は保証できません。
- JSONの末尾の改行未完了行は次回に持ち越します。完成済み行が不正な場合、または退避ファイルの最終行が未完了の場合は取得を失敗させ、読取り位置を保持します。
- 観測値は累積するため、削除通知は行いません。既知の値の再出現も通知しません。状態ファイルは値の種類に応じて増えます。
- 抽出条件の変更・項目追加では、そのファイルを先頭から再読取りします。変更していない項目の累積値は維持します。`--dry-run` は取得のみ行い、永続状態や通知を変更しません。
