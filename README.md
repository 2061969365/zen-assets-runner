# zen-assets-runner

上游 OpenCode CLI 的工具定义 / system prompt 抓包与漂移检测。

本仓库是**公开的 runner**：只跑 GitHub Actions（公共仓库分钟数不限），代码、工具定义、prompt 的**摘要**都公开；prompt 全文在私有仓库。

## 两个仓库的分工（契约 §8）

| 仓库 | 可见性 | 放什么 |
|---|---|---|
| `zen-assets-runner`（本仓库） | public | 抓包代码、抽取器、校验器、workflow、`assets/official_tools.json`、`assets/prompt-meta.json`（只有 sha256 / 字节数） |
| `zen-assets` | private | `assets/prompt.txt` 全文、完整资产存档 |

这样做的原因：

1. **公共仓库 Actions 分钟数不限**，抓包 + 单测 + 校验随便跑；私库只有 2000 分钟/月。
2. **prompt 正文不公开**。公开的只有它的哈希，所以"上游 prompt 有没有变"在公开的 git 历史里可见，内容不泄漏。
3. **不能直接把老仓库改 public**：`zen-assets` 的 git 历史里已经提交过 prompt 全文，改可见性等于连历史一起公开。

## 每天做什么

```
03:00 UTC  cron
  └─ 装 @opencode/cli@latest
  └─ 起本地 sink (127.0.0.1:19100)，把 CLI 的 baseURL 指过去
  └─ opencode run "Reply with the single word: ok"  →  抓到真实请求体
  └─ extract_assets.py   识别候选、切掉运行时尾部
  └─ validate_assets.py  8 条不变量 + prompt 规则
  └─ 脱敏门禁            Bearer / ses_ / ghp_ / sk- / 24-hex
  └─ 与 assets/ 比对     工具集合 / 字段 / order
  └─ 有漂移 → 固定分支 asset-drift/latest + 复用同一个 PR
```

四态：`NO_CAPTURE` / `EXTRACT_FAIL` / `DRIFT` / `NO_DRIFT`。前两种开 issue 并 fail，后两种分别开 PR 或正常退出。

## 闸门在产出方，不在 PR 上

`GITHUB_TOKEN` 推的分支触发的工作流会停在 `action_required`（需要人工批准），所以 PR 上的 `ci` 不会自动跑。因此 `refresh-assets` 自己第一步就跑：

```
python3 -m unittest discover -s tests -v
python3 tools/validate_assets.py --assets assets --strict --tools-only
```

**PR 只是给人看的差异报告，不是校验点。**

## 本地跑

```bash
python -m unittest discover -s tests -v
python tools/validate_assets.py --assets assets --strict --tools-only

# 用已有抓包离线抽一次
python tools/extract_assets.py --captures <dir> --out <dir> \
  --cli-version "opencode v2.3.0" --report-json report.json
```

## 已知限制

- 这条链路证明的是"CLI 发什么"，不是"上游收什么"。上游是否接受仍必须由真实出口验证，两者不能互相替代。
- `schedule` 只在默认分支生效；仓库连续 60 天无活动会被 GitHub 自动停用定时任务。
- **prompt 只报不动**（契约 §7.12）。抓包场景与生产注入场景不同构（实测同一天 8012 vs 94858 字节），自动覆盖等于删掉 87KB 正文。本仓库记录 `prompt-meta.json` 的 sha/字节数，正文更新是私有仓库里的人工动作。
- 工具名字集合的差异**不自动合并**（例如 `patch` vs `experimental_image_generate` 取决于 CLI 调用方式），由人判断。
