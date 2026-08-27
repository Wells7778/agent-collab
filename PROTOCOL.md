# PROTOCOL.md — Agent Hub 協作協定 v1

> 每個 agent session 開始時必讀全文;違反協定的輸出會被 daemon 拒收。
> 制定:2026-08-26(Phase 1)。Hub 即本 repo。

## 1. 系統角色與寫入權(單一寫入者原則)

| 角色 | 唯一取件來源 | 可寫入 |
|---|---|---|
| scheduler daemon | tasks/backlog/ | tasks/in-progress・blocked・review・done・invalid/、status/、events/events.jsonl、messages/(代寫 agent 提問)、knowledge/(rsync projection)、任務檔 append(代寫 agent 回報) |
| dashboard 後端 | tasks/review/、tasks/blocked/ | tasks/backlog/(新增、打回、解卡退回)、tasks/done/(完成與取消)、events/dashboard.jsonl、任務檔 append(人類回覆與打回意見)、review 型新任務檔 |
| agent(你) | — | ①自己的 workspace(cwd 之內)②stdout 回報。**永不直接寫 Hub** |

- Hub 寫入一律 temp + rename(同 filesystem);唯 append-only 的 events jsonl 例外——單一寫入者逐行 append + fsync。兩個 events 檔各自單一寫入者,dashboard 讀取時按 ts 合併。
- 例外:Claude Code/Codex 的 PreCompact hook 直接寫 handoffs/(hook 由系統注入,不算 agent 自主寫入)。

## 2. 任務狀態 = 所在目錄(唯一真相)

```
backlog → in-progress/<agent> → review → done
              ↓          ↑
           blocked →(人回覆)→ backlog
              └─(人丟棄)→ done(cancelled)
```

- 檔內 `status` 欄位僅供顯示;有分岔一律以目錄為準。
- 取消:`status: cancelled`,檔案在 done/;來源為 review/(人取消)或 blocked/(人丟棄不做)。
- schema 驗證不合格 → invalid/ 並記事件。

## 3. 任務檔

檔名 = `<id>.md`。YAML frontmatter + 三個 body 區塊:

```yaml
---
id: T-20260826-001
type: coding                # coding | review(互審)| research(查資料)| explore(盤點程式碼)
title: 一句話說清楚要做什麼
source:
  type: manual              # manual | asana(內容出自某張 Asana 票)
  asana_url: null           # 僅供人類追溯,不自動同步
project: my-service         # 對應 projects.yaml key
workspace:
  repo: null                # 派工時 daemon 自 projects.yaml 填入
  branch_base: null         # 同上
  branch: null              # 派工時填 agent/<name>/<id>-g<generation>
skills_required: [rust]
priority: P2                # P0 | P1 | P2 | P3
depends_on: []              # 所列 id 全在 done/ 且非 cancelled 才派
assigned_to: null           # 有值直接指定 agent,繞過能力路由
related_task: null          # review 型任務指向原任務 id
claimed_by: null
claimed_at: null
generation: 0               # 每次回收 +1(fencing:舊 session 分支不撞新派工)
status: backlog             # 僅供顯示:backlog|in-progress|blocked|review|done|cancelled
---

## 需求描述
## 驗收標準
## 執行報告
```

- 「執行報告」只由 daemon(代寫 agent 回報)與 dashboard 後端(人類回覆、打回意見)append;建檔時留空。
- 權威 schema = `agenthub/schema.py`(pydantic,daemon 與 dashboard 共用);範本 = `templates/T-000-template.md`。

## 4. Agent 執行契約(你要遵守的全部)

1. 啟動注入,依序是:本協定全文 → **你的角色定義** → knowledge projection 路徑(唯讀)→ 任務檔全文 → 最新 handoff(若有)。三層各有分工,不會互相覆蓋:本協定寫檔案系統契約、回報格式與狀態機;角色定義寫你的職責、產出格式與越界禁令;knowledge 寫專案慣例與領域知識。三者衝突時以本協定為準,並在回報裡指出衝突。
2. 有 handoff 先讀,從斷點續作;禁止重做已完成項。
3. 只在任務檔 `workspace.branch` 指定的分支工作;禁止碰其他分支、禁止 force push、禁止動 base 分支。
4. 跨專案知識只查給定的 projection 路徑(hub/knowledge/<slug>/);禁止讀寫 obs 本體(~/Documents/obs)。
5. secrets 已由 daemon 佈建;禁止從任何遠端(S3、presigned URL、自訂網域)拉取 env/憑證檔。
6. 里程碑 checkpoint:每完成一個里程碑輸出一個 checkpoint 回報(§5);長任務至少每個驗收條目一次。
7. 卡關:需要人類決策(需求矛盾、權限不足、環境壞掉)→ 輸出 blocked 回報(question 寫清楚問題與已嘗試方案)後結束 session;禁止空轉瞎猜。
8. 完成(type: coding):依驗收標準跑測試(關鍵輸出行放進報告)→ push `workspace.branch` → 開 CodeCommit PR:先查同分支既有 open PR,有則沿用其 URL,無則 create(冪等;`aws codecommit create-pull-request`,無 GitHub 無 `gh`)→ 輸出 final 回報。
9. 完成(type: review):checkout 指定 branch 審查;不開 PR、不留 PR comment;完整審查報告放 final 回報的 report_md。
10. 完成(type: research / explore):查資料或盤點程式碼;禁止改碼、禁止 commit/push、不開 PR。產出放 final 回報的 report_md——research 的每個關鍵事實附來源 URL,explore 的每個位置附 `檔案:行`;查不到就寫查不到,禁止憑記憶補。
11. 誠實回報:測試紅就說紅;做不到就 blocked;禁止樂觀宣稱、禁止無證據的「應該沒問題」。

## 5. stdout 回報格式(你唯一的回報通道)

fenced code block,語言標記固定 `hub-report`,內容為單一 JSON object:

~~~
```hub-report
{"kind": "checkpoint", "task_id": "T-20260826-001", "summary": "一句話", "report_md": "本里程碑增量(markdown)", "result": null, "pr_url": null, "question": null}
```
~~~

| kind | 必填欄位 | 效果 |
|---|---|---|
| checkpoint | summary, report_md | daemon 即時 append 進任務檔執行報告 |
| blocked | question | daemon 代寫 messages/ 一則,任務移到 blocked/ |
| final | result(`completed`/`failed`), summary, report_md;coding 完成另必填 pr_url | completed:coding/research/explore → review/、review 型 → done/;failed → review/ 由人裁決 |

- daemon 以 stdout 中「最後一個 hub-report block」為 session 結論;之前的 checkpoint 各自即時生效。
- coding final 的 pr_url 必填目前由人工於 review 把關;機械強制留待 PR 佈建階段實作。
- session 結束而無合法 final 回報 = 視同 agent 死亡,任務回收重派(generation +1)。
- JSON 不得含註解;無值填 null。
- `report_md` 的換行請寫 `\n`;寫成裸換行 daemon 也吃(解析用 `strict=False`),但別依賴這個寬容。

## 6. handoff 檔(context 壓縮保險)

路徑 `handoffs/<task-id>.handoff-<n>.md`(n 自 1 遞增)。四節,缺一不可:

```
## 已完成
## 做法與關鍵決策
## 還缺什麼
## 注意事項
```

## 7. messages(agent 提問載體)

- daemon 從 blocked 回報代寫 `messages/<ISO-ts>-<agent>-human.md`(一則一檔,append-only)。
- 人類在 dashboard 回覆 → 回覆 append 進任務檔執行報告 → 任務退回 backlog 重派;新 session 讀任務檔即見回覆。
- agent 之間不直接對話;溝通實質是 agent→人。

## 8. 系統 schema(權威定義:agenthub/schema.py)

### status/<agent>.json(daemon 寫,心跳 60 秒)

```json
{"agent": "claude", "state": "working", "task_id": "T-20260826-001", "project": "my-service", "phase": "running", "pid": 12345, "pgid": 12345, "started_at": "2026-08-26T10:00:00+08:00", "heartbeat_at": "2026-08-26T10:05:00+08:00"}
```

- state:`working` | `idle` | `resting`(撞到用量上限,休息至 `cooldown_until`)| `offline`(capability probe 失敗或 disabled);非 working 時 task_id/project/phase/pid/pgid/started_at 為 null。
- phase:`setup` | `running` | `reporting`。

### events/events.jsonl(daemon 寫)與 events/dashboard.jsonl(dashboard 後端寫)

每行一個 JSON:

```json
{"ts": "2026-08-26T10:00:00+08:00", "actor": "daemon", "event": "task_dispatched", "task_id": "T-20260826-001", "agent": "claude", "detail": {}}
```

事件類型——daemon:`daemon_started` `probe_failed` `task_invalid` `task_dispatched` `agent_spawned` `spawn_failed` `task_checkpoint` `report_parse_failed` `task_no_report` `task_blocked` `task_review_ready` `task_done` `agent_exited` `agent_timeout` `agent_rate_limited` `task_requeued`;dashboard:`task_created` `task_replied` `task_returned` `task_completed` `task_cancelled` `review_task_created`。

`report_parse_failed` 每個格式錯誤的 `hub-report` 區塊發一次(detail.error 含原因與內容片段);`task_no_report` 在 agent 退出但沒有任何 final/blocked 報告時發一次(detail 含 `report_blocks` / `parse_errors` / `stdout_bytes`)。兩者合起來區分「agent 根本沒寫報告」與「寫了但格式錯」。

## 9. 運行參數(config.yaml)

- 全域併發 ≤ 4;同一 `(project, branch_base)` 的**寫入型**任務同時 ≤ 2;同一 agent 同時 ≤ 1。
- `review` 任務是唯讀的,不佔寫入槽;寫入槽的 key 一律取自 projects.yaml 的 `default_branch`,不看任務檔上可能過期的 `workspace.branch_base`。
- 人類介入(dashboard 的 reply 與 return)把任務送回 backlog 時 generation 歸零——介入等於提供新資訊,重試預算重新計算。
- `assigned_to` 只繞過能力路由(skills 比對與 allowed_agents);agent enabled 與各併發上限是系統不變量,一律強制。
- **成對驗收**:coding 任務回報 completed 時,daemon 比對變更檔案與 projects.yaml 的 `spec_paths`。命中就自動生成 verifier + test-auditor 兩張互審票,且強制指派給與實作者**不同 runtime** 的角色;湊不出兩個獨立審查者、或連變更檔案清單都取不到(git 失敗)時,都發 `review_pair_unavailable` 事件而不降級放行——取不到清單會讓判定退化成「沒動測試」,若靜默處理等於保證無聲消失。兩張票都通過後原任務仍留在 review/ 等人裁決——「存活的突變是否可接受」是人的判斷,不是 hub 的。
- 任務逾時 120 分鐘(系統睡眠不計,喚醒後給 grace period);心跳 60 秒。
- 回收:kill pgid → 未提交 WIP commit 成 checkpoint → generation +1 → 退回 backlog(沿用原 worktree,自分支尖端續作)。
- generation 達 max_generation(預設 3)仍失敗 → 任務移到 blocked/ 並代寫 message,交人裁決(刻意保留 claimed_by 與 workspace.branch 供人工鑑識;重派時由 daemon 全量覆寫)。
- done/cancelled 任務的 worktree 保留 7 天後清理。

## 10. 邊界聲明

config.yaml 的 `agents:` 條目是**角色**(implementer / verifier / test-auditor / explorer / researcher),不是 LLM 廠牌;底層跑哪個 CLI 由該角色的 `runtime` 與 `command` 決定(`runtime` 對 enabled 的角色是必填,漏填直接載入失敗)。**`runtime` 與 `command` 的一致性沒有機械檢查**——因為 CLI 常經 wrapper 轉接,自動比對會誤殺合法設定。這是靠人維持的不變量:填錯不會有人發現,但「成對驗收跨廠牌」的保證會靜默失效。同一個 runtime 可以承載多個角色,不同角色也可以換到別的 runtime——這正是成對驗收能跨廠牌執行的前提。

knowledge projection 與本協定是**維護邊界,不是安全邊界**:同帳號下防意外不防惡意。需要真隔離時升級獨立 OS user 或容器。
