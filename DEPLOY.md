# 部署说明（2026-11-19 起）

线上目录 `/opt/pattern-workbench/app` 就是这个仓库的检出副本。
**推上去 = 上线**，不需要再 scp、不需要手动重启。

## 日常三条命令

```bash
cd C:/Users/Administrator/WorkBuddy/多图生图工作流/app-git

git pull --rebase origin master     # ① 开工前先拉，确保手上是最新
# …改你自己的文件（只改你负责的目录）…
git add -A && git commit -m "fk: 改了对比滑块标签"   # ② 提交
git push origin master              # ③ 推送 → 服务器自动检出 + 重启服务
```

推送成功时你会看到：

```
remote: [deploy] 已检出 a1b2c3d  fk: 改了对比滑块标签
remote: [deploy] ✅ pattern-workbench 运行中
```

## 为什么不会互相覆盖

服务器配置了 `receive.denyNonFastForwards=true`，`pre-receive` 钩子再挡一层：
**你手上的分支如果缺少线上已有的提交，推送会被直接拒绝**，提示你 `git pull --rebase`。

所以 A 任务推完，B 任务再推时必须先把 A 的提交合进来——A 和 B 永远共存，不会被抹掉。

## 目录归属（各改各的，不要跨界）

| 归属 | 路径 |
|---|---|
| 详情页复刻 | `fk/` |
| 商品替换 | `swap/` |
| 详情页生成 | `dtl/` |
| AI 生图 | `aigc/` |
| 共享文件（改前先打招呼） | `server.py` `accounts.py` `pipeline.py` `ai_sites.py` |

## 不进仓库的东西

`data/` `deliver/` `models/` `venv/` `duotu/` 所有 `*.bak*`、`_bak_*/`、`__pycache__/`，
以及**明文密钥**（`.apib_key`、`ai_sites.json`、`*cos_config.json`）。改这些请直接上服务器，别提交。

## 出事怎么办

- 线上被人手改过 → post-receive 会先把这些文件归档到 `/opt/pw-git/dirty-archive/<时间戳>/` 再同步，不会静默丢
- 想回滚 → `git revert <commit>` 或 `git reset --hard <commit> && git push --force`（**force 会被服务器拒绝**，这是故意的；回滚请用 revert）
- 服务没起来 → `journalctl -u pattern-workbench -n 50`
- 不走 git 的单文件小改 → 用 `dst-rebuild/build/deploy_guard.py` 兜底（会拦 `BEHIND`/`DIVERGED`）