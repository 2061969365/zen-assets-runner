# 资产格式契约（v1）

本文件是 `extract_assets.py`、`validate_assets.py`、`refresh-assets.yml` 三方共同遵守的唯一格式定义。**任何一方改动都必须先改本文件。**

来源：`zen-direct-v2/docs/impl-plan-v2.md` §A1 / §A1b / §C。

---

## 1. 目录布局

```
assets/                     # 当前生效资产（只由 PR 合入，任何自动化不得直推）
  official_tools.json
  prompt.txt
```

`assets/` 是**唯一真源**。网关侧会把这两个文件 vendor 进自己的仓库，本仓库不负责运行时。

---

## 2. `assets/official_tools.json`

```json
{
  "cli_version": "2.2.5",
  "captured_at": "2026-11-17T00:00:00Z",
  "source": {
    "workflow": "refresh-assets",
    "run_url": "https://github.com/2061969365/zen-assets/actions/runs/123",
    "sha256": "<sha256 of the tools object, canonical json>"
  },
  "order": ["edit", "glob", "...", "patch"],
  "tools": {
    "read": {
      "description": "…",
      "parameters": {"type": "object", "properties": {}},
      "strict": false
    }
  }
}
```

### 2.1 字段规则

| 字段 | 类型 | 规则 |
|---|---|---|
| `cli_version` | string | 抓包那次 `opencode --version` 的**实际输出**，禁止硬编码 |
| `captured_at` | string | RFC3339 UTC，抓包时刻 |
| `source.workflow` | string | `refresh-assets` \| `extract` \| `bootstrap` |
| `source.run_url` | string\|null | Actions run 链接；本地生成时为 null |
| `source.sha256` | string | 见下方 §2.1.1，**必须逐字按该定义算** |
| `order` | string[] | **显式有序**，决定出站顺序。**缺失即错误**，不得回退到 map 迭代顺序 |
| `tools` | object | 键 = 工具名 |
| `tools[*].description` | string | 原样保留，不得脱敏、不得截断 |
| `tools[*].parameters` | object | JSON Schema，原样保留 |
| `tools[*].strict` | bool | 抓包里有就保留，没有就 `false` |

### 2.1.1 `source.sha256` 的精确算法

```python
json.dumps(tools, sort_keys=True, separators=(',', ':'))   # ensure_ascii 保持默认 True
# → .encode("utf-8") → hashlib.sha256().hexdigest()
```

**`ensure_ascii` 必须是默认的 `True`，分隔符必须是 `(',', ':')`。** 这两点任何一个搞错都会算出不同哈希：

- 用默认分隔符（`', '` / `': '`）+ `ensure_ascii=False` → `91ac20f0…`（**错**，2026-11-17 上午那份资产就是这么错的）
- 按本定义 → `98b43520…`（对）

工具描述里出现过 U+2011（`stripped‑down`）这类非 ASCII 字符，所以 `ensure_ascii` 不是无关紧要的开关。


### 2.2 不变量（校验必须强制）

1. `order` 非空，长度 ≥ 8
2. `set(order) == set(tools.keys())` —— 两边必须完全一致，多一个少一个都是错
3. `order` 无重复
4. 每个 `tools[k].description` 非空
5. 每个 `tools[k].parameters` 是 object 且含 `"type": "object"`
6. `captured_at` 不得超前当前时间 30 天
7. `source.sha256` 与按规则重算的结果一致
8. **禁止出现 `Today's date:` 或 `Working directory:`** —— 出现在 tools 里说明抽错了东西

### 2.3 名字集合的取舍

`order` = `CLI 抓包名字 ∩ 我们已有真定义的名字`。

- 抓包里有、但我们没有真定义的名字 → **不出站、不写入**，只在 `validate_assets.py` 的告警里列出来
- 我们有定义、但抓包里没有的名字 → 以抓包为准，**移出**（例如 `experimental_image_generate`）
- 代码里如果有名字但资产里没有，网关侧按 `omit` 处理：不追加该名字

这条的理由：官方名 + 空 schema 会变成"诱饵工具"，和真实工具抢模型注意力。宁可少一个名字，也不要伪造一个。

---

## 3. `assets/prompt.txt`

纯文本，UTF-8，无 BOM。

**只允许静态正文。** 运行时尾部（`<env>…</env>`、`Today's date: …`、`Working directory: …`、`Here is some useful information…`）由网关自己拼，**不得入库**。

### 3.1 切分规则

抓到的 `messages[0].content` 是"静态正文 + 运行时尾部"的整体。抽取时必须：

```
找到第一个出现在行首的 "Today's date: " 或 "<env>"
从该处之前的最后一个换行切断，保留前半段
```

### 3.2 校验规则

1. 长度 ≥ 2000 字符
2. **不得包含** `Today's date:`、`Working directory:`、`Platform:`、`<env>`
3. 必须以**恰好一个** `\n` 结尾；禁止尾部空行（`\n\n` 结尾）、行尾空格、行尾制表符
4. 记录 sha256 到 PR 描述

> 第 3 条 2026-11-17 修正：原文写"不得以空白结尾"，字面执行会把合法的 POSIX 单换行结尾也判错。正确规则是"恰好一个换行 + 无尾部空行"。

### 3.4 `experimental_image_generate` 的处理

该名字抓包里有、定义文件里没有，按 §2.3 故意移出。**它不产生警告，`--strict` 也必须能通过真实资产。**

判定逻辑：
- "期望出站名字集合" = `assets.official_tools.json` 的 `order`，校验对象是它自身的一致性，不是与上游名单比对
- 上游名单（含 `experimental_image_generate`）只用于**抓包候选过滤**，不用于资产告警
- 若要提醒"上游有而资产没有"，用 `--compare-upstream <file>` 显式开启，默认关闭


### 3.3 为什么要这么严

2026-10-28 的生产事故：把抓到的整段（含抓包机的 `/home/runner/work/...` 与当天日期）静态存进二进制，再叠运行时尾部 → env 段重复 + 把 CI 环境固化进生产 prompt，全站 429。

参考：`docs/upstream-gate-notes.md` §8.8（在 zen-direct-v2 仓库）。

---

## 4. 抓包候选识别规则

CLI 一次会发多条请求，必须挑对：

```
按 rec["seq"] 升序
→ method == POST
→ path 匹配 ^/inference/v1/(chat/completions|responses)$
→ 请求头 User-Agent 匹配 ^opencode/latest/[\d.]+/cli$
   （或存在 x-opencode-client；用于排除我们自己网关的请求）
→ body 解析成功，且含 "messages" 或 "input"
→ 取 role in {system, developer} 的消息内容
→ tools 数 >= 3，且每个名字都在"已知官方名"集合里（不认识的名字只报告，不静默接受）
→ 在全部候选中取 (tools 数, system 长度) 最大的那条
→ 按 body 的 sha256 去重（CLI 重试会产生同一个 body 的多份）
```

**不要取"第一条"。** 实测 CLI 会先发标题生成请求（`tools=0`，system 约 1806 字节），还有 compaction、plan 模式子集、`/v1/responses` 形状（走 `input` 而不是 `messages`）。

已知官方名（用于候选过滤，不是资产内容）：

```
edit execute glob grep patch question read shell skill subagent webfetch websearch write
experimental_image_generate
```

---

## 5. 失败语义（四态，禁止静默）

| 状态 | 条件 | 退出码 | 动作 |
|---|---|---|---|
| `NO_CAPTURE` | 0 条候选 | 1 | 打 `no-capture` 标签开 issue |
| `EXTRACT_FAIL` | 有候选但字段缺失/自检不过 | 1 | 打印候选 seq 与失败原因 |
| `DRIFT` | 与 `assets/` 有差异 | 0 | 开 PR（label `asset-drift`） |
| `NO_DRIFT` | 无差异 | 0 | 什么都不做 |

"上游改了请求形状"是最有价值的信号，**必须让它以 exit 1 暴露**，不能被静默跳过。

---

## 6. 脱敏（四层，缺一不可）

1. **采集层**：`sink.py` 落盘的请求头里，除 `user-agent` 外的值一律替换为 `<redacted>`
2. **提取层**：只输出 `order`/`tools`/`description`/`parameters`/`strict`，**不输出任何请求头**
3. **提交前**：对 `assets/` 全文 grep `Bearer |ses_|ghp_|sk-|[0-9a-f]{24}`，命中即 fail
4. **artifact**：只上传 `assets_candidate/`，原始 captures **不上传**

实测头里有 `x-opencode-project`（稳定 24 hex）、`x-opencode-session`、`x-session-id`；`zen-direct-v2/.gitignore` 已写明 captures 含凭证不入库。

**第 3 层 grep 必须排除 `source.sha256` 自身**：该字段是 64 位十六进制，包含 24 位子串，裸 `grep -E '[0-9a-f]{24}'` 会自我命中。用前后边界匹配，或在扫描前剔除 `source.sha256` 字段。

---

## 7. 2026-11-17 契约澄清（优先于前文，冲突时以本节为准）

| # | 歧义 | 定稿 |
|---|---|---|
| 7.1 | `source.workflow` 允许值 | `refresh-assets` \| `extract` \| `bootstrap`，三者都合法 |
| 7.2 | `source.sha256` 的 `ensure_ascii` | 见 §2.1.1：**默认 True + 紧凑分隔符**。基线旧值 `91ac20f0…` 已作废 |
| 7.3 | §3.2-3「行尾空格/制表符」作用域 | **只检查最后一行**。真实 prompt 第 1560 行（`… compaction 保留 --> `）本身以空格结尾，那是上游原文，不许改 |
| 7.4 | 抓包中出现未知工具名 | **整条请求不算候选**，并在 stderr 打印完整名单。理由：上游加工具是重大信号，表现为 `NO_CAPTURE` + 名单比"悄悄剔掉"响亮得多 |
| 7.5 | 同一请求内重复工具名 | 整条不算候选 |
| 7.6 | 抓包 path 带 query string | 按 `^/inference/v1/(chat/completions\|responses)$` **全串匹配**，带 `?` 即不匹配 |
| 7.7 | system 消息缺失 | **不淘汰候选**，到出站自检阶段判 `EXTRACT_FAIL`（否则会误报 `NO_CAPTURE`） |
| 7.8 | `captured_at` 取值 | 取所选记录的 `ts` 字段；缺失才用当前时间，以保证可复现 |
| 7.9 | 退出码 | extract 只用 0 / 1；**`NO_CAPTURE` 与 `EXTRACT_FAIL` 都退 1**，靠 stderr 的 `ERROR NO_CAPTURE` / `ERROR EXTRACT_FAIL` 标记或 `--report-json` 的 `status` 区分。workflow **不得**按退出码数值分类 |
| 7.10 | `--report-json` | 正式纳入契约。结构 `{"status": "...", "candidates": n, "excluded": {原因: 次数}, ...}`，workflow 应传 `--report-json "$W/extract_report.json"` |
| 7.11 | `--compare-upstream` 格式 | `-` = 内置 §4 名单；或 JSON 数组；或含 `order`/`tools` 的对象。默认关闭 |

### 7.12 新增 §3.5：`prompt.txt` 只做告警，**永不自动替换**

2026-11-17 首次真实跑通后新增的硬规则。

同一份 CLI、同一天，两个场景抓到的 system 长度：

| 场景 | system 字节 |
|---|---|
| 干净 runner、裸仓库、`opencode run "ok"` | **8012** |
| 完整项目（含 AGENTS.md / Memory / available_skills） | 88342 |
| 生产注入的静态正文 | 94858 |

**prompt 内容随运行场景变化，不是稳定资产。** 所以：

1. `refresh-assets` **不得**把候选 prompt 覆盖到 `assets/prompt.txt`
2. prompt 差异只在 PR body / step summary 里作为**告警**呈现，供人判断
3. 增加体量护栏：候选 prompt / 当前 prompt 的比值 `< 0.5` 或 `> 2.0` 时，标记 `SCENARIO_MISMATCH`，连告警都要降级为"疑似场景不同，忽略"
4. 只有人明确判断"上游真的改了静态正文"并手改 `assets/prompt.txt`，改动才生效

换句话说：**工具集合自动跟进，prompt 只报不动。**

### 7.13 首次真实运行的关键发现（2026-11-17，run 44042615789）

干净 runner 上 CLI v2.3.0 实际发出 13 个工具，与我们的基线对比：

```
新增: experimental_image_generate
删除: patch
改动: read(parameters) / subagent(description, parameters) / execute(description)
```

**这证实了 `patch` 与 `experimental_image_generate` 的出现在不同 CLI 配置/版本下是互斥的。** 我们基线取的是含 `patch`、不含 `experimental_image_generate` 的一侧，而生产实测 24/24 全 200——说明两侧上游都接受。

结论：名字集合的差异**不自动合并**，由人判断。这也正是当初决定不做自动轮换的原因。

## 8. 两仓库分工（2026-11-17 定稿）

抓包链路要跑在**公共**仓库上才有不限量的 Actions 分钟数，但 prompt 全文不宜公开。因此拆成两个仓库：

| 仓库 | 可见性 | 内容 |
|---|---|---|
| `zen-assets-runner` | public | 代码（`tools/` `tests/` `schema/`）、workflow、`assets/official_tools.json`、`assets/prompt-meta.json` |
| `zen-assets` | private | `assets/prompt.txt` 全文、完整资产存档 |

### 8.1 为什么不能把老仓库直接改 public

`zen-assets` 的 git 历史里已经提交过 `prompt.txt` 全文（`4db065f` 起）。改可见性会把**历史**一并公开，等于既公开内容又公开时间线。必须新建一个干净历史的仓库。

### 8.2 `assets/prompt-meta.json`

```json
{
  "sha256": "...", "bytes": 102499, "chars": 94858,
  "cli_version": "...", "captured_at": "...",
  "previous_sha256": "...", "previous_bytes": 102499
}
```

公开的只有哈希与长度。**"上游 prompt 有没有变"在公开 git 历史里可见，正文不泄漏。**

### 8.3 硬规则

1. public 仓库**不得**出现 `prompt.txt` 全文，包括测试数据、artifact、日志
2. artifact 只上传 `official_tools.json` + `extract-report.json` + `prompt.sha256`，**不得**上传 candidate 目录
3. 测试用合成正文（`SYNTHETIC_PROMPT`）覆盖 prompt 的三条规则，不用真实正文
4. `validate_assets.py --tools-only` 用于跳过 prompt 检查；public 仓库的 CI 与自检必须带这个 flag
5. prompt 全文的更新是私有仓库里的**人工动作**，`refresh-assets` 永远只写摘要

### 8.4 何时仍会泄漏

公开 run 的日志里有：工具名、字节数、sha、排除原因。**没有** prompt 正文、没有请求头、没有 token。脱敏门禁仍然逐轮执行。



两个实现方（extract / validate）在落地时提出了 11 条歧义，逐条定稿：
