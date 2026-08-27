# Agent Hub — 多 Agent 協作調度系統

單機上以「純檔案 + 腳本」運作的多 agent 調度中心。任務由 dashboard 建立,scheduler daemon 作為唯一決策點派工給**角色**(implementer、verifier、test-auditor、explorer、researcher),每個角色底下綁一個 headless CLI(Claude Code、Codex、本機 gemma、Gemini),完成後交人裁決。

沒有資料庫、沒有訊息佇列:`tasks/` 底下的目錄就是狀態機,git 就是稽核歷史。

- 協作協定(agent 啟動時全文注入):[`PROTOCOL.md`](PROTOCOL.md)

## 需求

- macOS(用到 `caffeinate`、launchd;核心邏輯本身跨平台)
- Python 3.12+
- git
- 至少一個 agent CLI:[`claude`](https://claude.com/claude-code)、`codex`、`hermes`(本機模型)、`agy`(Gemini 查詢)。成對驗收需要至少兩個不同 CLI

## 快速開始

```sh
git clone <this-repo> agent-collab && cd agent-collab

# 1. 環境
uv venv && uv pip install -e '.[dev]'
# 沒有 uv:python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

# 2. 設定(兩個檔都不進版控)
cp config.example.yaml config.yaml
cp projects.example.yaml projects.yaml

# 3. 驗證
.venv/bin/pytest -q          # 195 passed
```

編輯 `config.yaml`:把你**實際裝了** CLI 的角色設成 `enabled: true`,其餘留 `false`。
編輯 `projects.yaml`:註冊你要讓 agent 動的 repo。兩個檔的每個欄位在範本裡都有註解。

啟動:

```sh
source .venv/bin/activate
bin/hub-dashboard --hub-dir . --port 8642   # http://127.0.0.1:8642
bin/hub-daemon                              # 另一個終端機;--hub-dir 預設 repo 根
```

daemon 啟動時會 probe 每個 enabled agent(跑它的 `probe` 命令),不通的當次自動停用並記一筆 `probe_failed`。

## 使用

**建立任務**:dashboard 的「新增任務」表單(id 由後端產生 `T-YYYYMMDD-NNN`)。進階用法是照 [`templates/T-000-template.md`](templates/T-000-template.md) 手寫任務檔丟進 `tasks/backlog/`;daemon 派工前會重驗 schema,不合格的移到 `tasks/invalid/`。

**任務生命週期**:

```
backlog → in-progress/<agent> → review →(人按完成)→ done
              ↓          ↑
           blocked →(人回覆)→ backlog
              └─(人丟棄)→ done(cancelled)
```

**Blocked 兩個動作**:回覆(回覆 append 進任務檔後退回 backlog 重派)、丟棄不做(標記 `cancelled` 移入 done/,不再派工)。無效或已無意義的卡關任務不必硬解,直接丟棄。

**任務型別**決定 agent 做什麼、以及誰接得到:

| type | 用途 | 完成時 |
|---|---|---|
| `coding` | 改程式 | push 分支 + 開 PR,進 review 等人裁決 |
| `review` | 互審(由 dashboard 從既有任務產生) | 審查報告寫回任務檔,直接進 done |
| `research` | 查資料,不改碼 | 答案(附來源 URL)寫進報告,進 review |
| `explore` | 盤點程式碼,不改碼 | 位置清單(附 `檔案:行`)寫進報告,進 review |

路由同時看兩件事:角色的 `task_types` 要涵蓋該任務型別,`skills` 要涵蓋 `skills_required`。researcher 宣告 `task_types: [research]`,所以永遠不會收到寫程式的任務,反之亦然。

**成對驗收**:coding 任務完成時,daemon 比對變更檔案與 `spec_paths`。動到測試就自動生成 verifier(功能對不對)+ test-auditor(測試憑什麼證明功能對)兩張互審票,強制指派給與實作者**不同 runtime** 的角色。兩張都通過後原任務仍留在 review/ 等人裁決——「存活的突變是否可接受」是人的判斷。湊不出兩個獨立審查者時發 `review_pair_unavailable` 而不降級放行。

**Review 四個動作**:完成(移 done)、派互審(手動補派,同樣受不同 runtime 約束)、打回(意見 append 後退回 backlog,generation 歸零)、取消。coding 任務的完成判定是人工的,系統不輪詢 PR 狀態。

**即時觀察**:dashboard 上點桌位或 session 列可看該任務的即時輸出。

## 韌性

- **回收與 fencing**:agent 死亡或逾時(預設 120 分)→ kill process group → 把 WIP commit 成 checkpoint → generation +1 退回 backlog,沿用原 worktree 從分支尖端續作。超過 `max_generation` 轉 blocked 交人。
- **用量上限**:agent 撞到訂閱用量上限時不算失敗——它會進入 cooldown(對方有給恢復時間就用那個,否則用 `rate_limit_cooldown_minutes`),期間不被派工,任務以**原 generation** 退回 backlog,可由其他 agent 接手。cooldown 寫在 `status/<agent>.json`,daemon 重啟後仍生效。
- **併發上限**:全域、單一 `(project, default_branch)` 的寫入型任務、單一角色各自獨立設定。review 任務是唯讀的,不佔寫入槽。

## 常駐(launchd)

```sh
sed "s#__HUB_DIR__#$PWD#g; s#__EXTRA_PATH__#$HOME/.local/bin#" \
  bin/com.weiby.agent-hub.plist.example > ~/Library/LaunchAgents/com.weiby.agent-hub.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.weiby.agent-hub.plist
```

launchd 的環境很精簡,PATH 必須在 plist 內顯式宣告——agent CLI 常裝在 `~/.local/bin` 或 `/opt/homebrew/bin`,漏了 probe 就會全滅。日誌在 `logs/`。

## 目錄結構

| 路徑 | 用途 |
|---|---|
| `PROTOCOL.md` | 協作協定,agent 啟動時全文注入 |
| `config.example.yaml` / `projects.example.yaml` | 設定範本(實際的 `config.yaml`、`projects.yaml` 不進版控) |
| `agenthub/` | schema(pydantic)、hubfs、scheduler、provision、claude_runner、probe、dashboard_api |
| `templates/roles/` | 各角色的職責定義,由 config 的 `prompt` 欄位引用 |
| `bin/hub-daemon` | daemon 進入點(probe → startup_scan → 常駐 tick) |
| `bin/hub-dashboard` | dashboard 進入點(FastAPI + 單檔前端,只綁 127.0.0.1) |
| `bin/agy-hub-runner` | agy 轉接器(它的 prompt 走參數而非 stdin) |
| `tasks/ handoffs/ messages/ status/ events/ knowledge/` | 運行時狀態,不進版控 |
| `templates/` | 任務檔與 handoff 範本 |

## 接新的角色

在 `config.yaml` 的 `agents:` 加一個條目就好,不必改程式:

```yaml
your-role:
  enabled: true
  runtime: your-cli              # 底層 CLI,enabled 必填;成對驗收靠它判斷「不同廠牌」
  prompt: templates/roles/your-role.md
  skills: [general, python]
  task_types: [coding, review]   # 省略即為此預設值
  command: [your-cli, --headless, --whatever]
  probe: [your-cli, --version]
```

角色是「職責」,runtime 是「誰來跑」。同一個 runtime 可以承載多個角色(verifier 與 test-auditor 都跑 codex),同一個角色也能換到別的 runtime。

`runtime` 與 `command` 的一致性**沒有機械檢查**(wrapper 轉接讓自動比對會誤殺合法設定),填錯不會有人發現,但成對驗收的跨廠牌保證會靜默失效——改 command 時務必同步改 runtime。

`prompt` 指向的檔案會疊在 PROTOCOL.md 之後注入,只寫職責與產出格式——協定細節留給 PROTOCOL.md,專案慣例留給 `knowledge_paths`,三層不重疊。

契約只有三條:**prompt 從 stdin 進**、**回報從 stdout 出**(fenced code block,語言標記 `hub-report`,格式見 PROTOCOL §5)、**probe 命令成功要回 0**。

兩個實務上會踩到的坑:

- CLI 若有 TUI/markdown 渲染,可能會把 hub-report 的 fence 吃掉,回報協定就靜默失效——`hermes` 要靠 `-Q` 才行。接新 runtime 時務必先手動跑一次,確認輸出裡的 fence 完整。
- prompt 只吃參數不吃 stdin 的 CLI,包一層 wrapper 轉接即可,`bin/agy-hub-runner` 就是三行的範例。

## 安全邊界聲明

本系統的隔離是**維護邊界,不是安全邊界**(防意外,不防惡意)。agent 有完整 shell;worktree 只隔離 git 工作目錄,不隔離 SSH key、其他 repo 或網路。knowledge projection 與協定條文擋不住惡意指令,而任務內容本身就是 prompt injection 的入口。

範本裡的 `claude` 命令帶 `--dangerously-skip-permissions`、`hermes` 帶 `--yolo`,這是 headless 無互動提示的權宜之計。**首次無人值守執行前請先完成權限收緊評估**(deny 規則、`--permission-mode`、PreToolUse hook)。需要真正的隔離,請升級到獨立 OS 使用者或容器。
