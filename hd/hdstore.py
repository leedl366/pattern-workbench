# -*- coding: utf-8 -*-
"""高清修复版块 —— COS 图床（复用父应用 `storage.py` 的客户端，前缀独立）。

保存规则与「图案提取 / 批量去水印」完全一致（2026-10-15 补上，此前 hd 是**唯一没接 COS**
的板块：所有产物都走 `hd/api/tasks/<tid>/png|thumb|src|psd` 从服务器出口回源，实测出口
~1Mbps → 素材库对比图与 hd 页面本身都很慢）：

- **双写兜底**：本地 `hd/data/` 始终保留一份（作服务源与兜底）；COS 上传成功则对外给
  COS 公网直链，失败 / 未配置 → 返回 None，前端自然回退站内路由，行为不变。
- **前缀隔离**（与其它板块同规则，互不覆盖）：
      图案提取：线上 `pattern/`  本地 `pattern_dev/`
      去水印：  线上 `wm/`       本地 `wm_dev/`
      多图编辑：线上 `duotu/`    本地（duotu 自带 data/cos_config.json）
      高清修复：线上 `hd/`       本地 `hd_dev/`
  优先级：`HD_COS_PREFIX` 环境变量 > `cos_config.json` 的 `"hd_prefix"` > 自动推导
  （父前缀含 `_dev` → `hd_dev/`，否则 `hd/`）。
- 密钥不落新文件：统一读父应用的 `<app>/cos_config.json`。

对外 API：
    enabled()                     -> COS 是否可用
    prefix() / info()             -> 自身前缀与状态（自检用）
    key_for(rel)                  -> `output/u1/<tid>/hd.png` → `hd/output/u1/<tid>/hd.png`
    public_url(rel)               -> 该 rel 的公网 URL（不校验存在性）
    upload_rel(base_dir, rel)     -> 上传 base_dir 下的 rel，成功返回 URL，失败 None
    delete_task(uid, tid)         -> 删该任务整个 COS 前缀（本地清理/删除时同步调用）
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_HERE)
if _APP not in sys.path:                     # 独立运行时也能 `import storage`
    sys.path.insert(0, _APP)

try:
    import storage as _S                     # 父应用模块（同一 app/ 目录）
except Exception as _e:                      # noqa: BLE001
    _S = None
    print("[hd.hdstore] 父 storage 模块不可用，COS 关闭：%s" % _e)

HD_SUB = "hd/"
MAX_PER_RUN = 2000


def _norm_prefix(p) -> str:
    """"hd_dev" / "/hd_dev/" → "hd_dev/"；空 → "hd/"。"""
    p = str(p or "").strip().strip("/")
    return (p + "/") if p else HD_SUB


def _cfg_hd_prefix() -> str:
    """`cos_config.json` 里可选的 `hd_prefix` 字段（**缺失 / 为空 / 读不到 → ""**）。

    🔴 这里**不能**套 `_norm_prefix()`：它会把 None 兜成默认 `hd/`，于是
    `env or 配置 or 自动推导` 在「配置里没写 hd_prefix」时被 `hd/` 短路，
    `_auto_prefix()` 永远轮不到 —— 本地实例直接写进线上 `hd/` 命名空间（隔离失效）。
    """
    if _S is None:
        return ""
    try:
        with open(_S.CFG_PATH, encoding="utf-8") as f:
            v = str(json.load(f).get("hd_prefix") or "").strip().strip("/")
        return (v + "/") if v else ""
    except Exception:                        # noqa: BLE001
        return ""


def _auto_prefix() -> str:
    """父前缀带 `_dev`（本地实例）→ `hd_dev/`；否则 `hd/`。"""
    base = str(getattr(_S, "PREFIX", "") or "") if _S is not None else ""
    return "hd_dev/" if "_dev" in base else HD_SUB


# 🔴 隔离铁律：本地与线上共用同一个桶。本地 COS 前缀必须是 `hd_dev/`，
# 否则本地一上传就把测试图灌进线上 `hd/`，线上看全是孤儿。
PREFIX = _norm_prefix(os.environ.get("HD_COS_PREFIX") or _cfg_hd_prefix() or _auto_prefix())


def enabled() -> bool:
    return _S is not None and _S.enabled()


def prefix() -> str:
    return PREFIX


def info() -> dict:
    """自检信息：是否可用 / 桶 / 前缀 / 公网基址。"""
    out = {"enabled": False, "prefix": PREFIX, "bucket": "", "region": "", "base": "", "reason": ""}
    if _S is None:
        out["reason"] = "父 storage 模块不可用"
        return out
    try:
        c, cfg = _S._get()                   # noqa: SLF001 —— 同项目内部调用
    except Exception as e:                   # noqa: BLE001
        out["reason"] = str(e)
        return out
    if c is None or not cfg:
        out["reason"] = "COS 未配置或 SDK 缺失（已回退本地文件服务）"
        return out
    out.update({"enabled": True, "bucket": cfg.get("bucket", ""),
                "region": cfg.get("region", ""), "base": _S.public_url("")})
    return out


def _norm_rel(rel) -> str:
    return str(rel).replace("\\", "/").lstrip("/")


def key_for(rel) -> str:
    """`output/u1/<tid>/hd.png` → `hd/output/u1/<tid>/hd.png`"""
    return PREFIX + _norm_rel(rel)


def public_url(rel) -> "str | None":
    if not enabled():
        return None
    return _S.public_url(key_for(rel))


def upload_rel(base_dir: str, rel: str) -> "str | None":
    """上传 `base_dir/<rel>`，成功返回公网 URL，失败/文件不存在返回 None（调用方回退本地）。"""
    if not enabled():
        return None
    rel = _norm_rel(rel)
    fp = os.path.join(base_dir, rel.replace("/", os.sep))
    if not os.path.isfile(fp):
        return None
    return _S.upload_file(fp, key_for(rel))


def upload_task(uid, tid, rels) -> dict:
    """把一个任务的若干 rel 一次传完 → {rel: url}（实际传成功的才有键）。

    `rels` 允许带空值 / 不存在（`thumb_rel` 自带 `|err=...` 尾巴由调用方剥掉），
    单个失败只跳过不中止 —— 素材库对缺键的 rel 会自动回退站内路由。
    """
    out = {}
    if not enabled():
        return out
    base = os.path.join(_APP, "hd", "data")
    for rel in rels:
        if not rel:
            continue
        try:
            u = upload_rel(base, rel)
            if u:
                out[_norm_rel(rel)] = u
        except Exception:                    # noqa: BLE001
            pass
    return out


def delete_task(uid, tid) -> dict:
    """删任务时清掉 COS 上该任务整个前缀（尽力而为，失败只打日志）。"""
    out = {"deleted": 0, "keys": 0}
    if not enabled():
        return out
    prefix = key_for("output/u{0}/{1}/".format(uid, tid))
    try:
        objs = _S.list_all(prefix)
    except Exception:                        # noqa: BLE001
        return out
    keys = [o["key"] for o in objs if o.get("key")]
    out["keys"] = len(keys)
    if not keys:
        return out
    c, cfg = _S._get()                       # noqa: SLF001
    if c is None:
        return out
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        try:
            c.delete_objects(Bucket=cfg["bucket"],
                             Delete={"Object": [{"Key": k} for k in batch], "Quiet": True})
            out["deleted"] += len(batch)
        except Exception as e:               # noqa: BLE001
            if _S is not None:
                _S._log("hd 批量删除失败: %s" % e)   # noqa: SLF001
    return out


def sweep(keep_tids=(), ttl_hours: float = 72.0, dry_run: bool = False) -> dict:
    """前缀级孤儿兜底：删掉「不属于 `keep_tids` 且 COS 上最后修改早于 TTL」的对象。

    主清理路径是 `app.py::_sweep()` / `delete_tasks()`（本地删文件 → 同步删 COS）；
    本函数只兜底「本地已无、COS 仍留」的残留（上传成功但本地先被删、目录手工清理等）。

    防误删两条（与其它板块同规则）：
      ① `keep_tids` 为空且非 dry_run → **不执行**；
      ② 单次最多删 `MAX_PER_RUN` 个对象。
    """
    out = {"scanned": 0, "matched": 0, "deleted": 0, "bytes": 0, "keys": [],
           "dry_run": bool(dry_run), "skipped": ""}
    if not enabled():
        out["skipped"] = "COS 不可用"
        return out
    keep = set(str(x) for x in keep_tids)
    if not keep and not dry_run:
        out["skipped"] = "keep 为空，跳过（防误删）"
        if _S is not None:
            _S._log("hd " + out["skipped"])          # noqa: SLF001
        return out
    import time as _t
    cutoff = _t.time() - float(ttl_hours) * 3600.0
    doomed = []
    try:
        objs = _S.list_all(PREFIX)
    except Exception:                        # noqa: BLE001
        objs = []
    for it in objs:
        out["scanned"] += 1
        k = str(it.get("key") or "")
        # hd/output/u<uid>/<tid>/<file> —— tid 在倒数第二段
        parts = k.rstrip("/").split("/")
        tid = parts[-2] if len(parts) >= 4 else ""
        if tid in keep:
            continue
        mt = it.get("mtime")
        if mt is None or mt >= cutoff:
            continue
        doomed.append((k, it.get("size") or 0))
    out["matched"] = len(doomed)
    out["bytes"] = sum(s for _, s in doomed)
    out["keys"] = [k for k, _ in doomed][:200]
    if dry_run or not doomed:
        return out
    c, cfg = _S._get()                       # noqa: SLF001
    if c is None:
        return out
    for i in range(0, min(len(doomed), MAX_PER_RUN), 1000):
        batch = doomed[i:i + 1000]
        try:
            c.delete_objects(Bucket=cfg["bucket"],
                             Delete={"Object": [{"Key": k} for k, _ in batch], "Quiet": True})
            out["deleted"] += len(batch)
        except Exception as e:               # noqa: BLE001
            _S._log("hd 孤儿清理批量删除失败: %s" % e)   # noqa: SLF001
    return out
