# -*- coding: utf-8 -*-
"""批量去水印版块 —— COS 图床（复用父应用 `storage.py` 的客户端，前缀独立）。

保存规则与「图案提取」版块完全一致（2026-10-11 拍板）：

- **双写兜底**：本地 `wm/data/` 始终保留一份（作服务源与兜底）；COS 上传成功则对外
  给 COS 公网直链，失败 / 未配置 → 返回 None，前端自然回退本地相对路径，行为不变。
- **前缀隔离**（与图案提取同规则，且两侧互不覆盖）：
      图案提取：线上 `pattern/`   本地 `pattern_dev/`
      去水印：  线上 `wm/`        本地 `wm_dev/`
  优先级：`WM_COS_PREFIX` 环境变量 > `cos_config.json` 的 `"wm_prefix"` > 自动推导
  （父前缀含 `_dev` → `wm_dev/`，否则 `wm/`）。
  自动推导保证「本地开发实例永远不污染线上 COS」——本地无论怎么启动都落 `wm_dev/`。
- **保留 N 小时**：与图案提取同一 TTL（`WM_TTL_HOURS`，默认 72）。本地 `_sweep` 删掉
  的文件会同步删 COS 同 key；另有 `sweep()` 前缀级孤儿兜底（清掉本地已无、且 COS 上
  已过期的残留对象）。
- 密钥不落新文件：统一读父应用的 `<app>/cos_config.json`。

对外 API：
    enabled()                     -> COS 是否可用（SDK 缺失 / 未配置 → False）
    prefix() / info()             -> 自身前缀与状态（自检用）
    key_for(rel)                  -> `preview/compare_x.jpg` → `wm/preview/compare_x.jpg`
    public_url(rel)               -> 该 rel 的公网 URL（不校验存在性）
    upload_rel(base_dir, rel)     -> 上传 base_dir 下的 rel，成功返回 URL，失败 None
    upload_bytes(raw, rel, ctype) -> 同上（内存字节）
    delete_rels(rels)             -> 按 rel 批量删 COS 对象（本地清理时同步调用）
    sweep(keep_rels, ttl_hours)   -> 前缀级孤儿兜底清理
"""
from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_HERE)
if _APP not in sys.path:                     # 独立运行时也能 `import storage`
    sys.path.insert(0, _APP)

try:
    import storage as _S                     # 父应用模块（同一 app/ 目录）
except Exception as _e:                      # noqa: BLE001
    _S = None
    print("[wm.storage] 父 storage 模块不可用，COS 关闭：%s" % _e)

WM_SUB = "wm/"
MAX_PER_RUN = 2000


def _norm_prefix(p) -> str:
    """"wm_dev" / "/wm_dev/" → "wm_dev/"；空 → "wm/"。"""
    p = str(p or "").strip().strip("/")
    return (p + "/") if p else WM_SUB


def _cfg_wm_prefix() -> str:
    """`cos_config.json` 里可选的 `wm_prefix` 字段（**缺失 / 为空 / 读不到 → ""**）。

    🔴 同 hdstore：不能套 `_norm_prefix()`，否则字段缺失时被默认 `wm/` 短路，
    自动推导（本地 `wm_dev/`）失效 → 本地污染线上命名空间。
    """
    if _S is None:
        return ""
    try:
        with open(_S.CFG_PATH, encoding="utf-8") as f:
            v = str(json.load(f).get("wm_prefix") or "").strip().strip("/")
        return (v + "/") if v else ""
    except Exception:                        # noqa: BLE001
        return ""


def _auto_prefix() -> str:
    """父前缀带 `_dev`（本地实例）→ `wm_dev/`；否则 `wm/`。"""
    base = str(getattr(_S, "PREFIX", "") or "") if _S is not None else ""
    return "wm_dev/" if "_dev" in base else WM_SUB


# 🔴 隔离铁律：本地与线上共用同一个桶。本地的 COS 前缀必须是 `wm_dev/`，
# 否则本地一上传就把测试图灌进线上 `wm/`，线上看全是孤儿。
# 优先级：WM_COS_PREFIX env > cos_config.json 的 "wm_prefix" > 自动推导。
PREFIX = _norm_prefix(os.environ.get("WM_COS_PREFIX") or _cfg_wm_prefix() or _auto_prefix())


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
    """`preview/compare_a.jpg` → `wm/preview/compare_a.jpg`"""
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


def upload_bytes(raw: bytes, rel: str, content_type: str = "application/octet-stream") -> "str | None":
    if not enabled():
        return None
    return _S.upload_bytes(raw, key_for(rel), content_type)


def delete_rels(rels) -> dict:
    """按相对路径批量删 COS 对象（本地 72h 清理删文件时同步调用）。"""
    out = {"deleted": 0, "keys": 0}
    if not enabled():
        return out
    keys = [key_for(r) for r in rels if r]
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
                _S._log("wm 批量删除失败: %s" % e)   # noqa: SLF001
    return out


def list_all():
    """列出本版块前缀下全部对象 → [{"key","size","mtime"}]。"""
    if not enabled():
        return []
    return _S.list_all(PREFIX)


def sweep(keep_rels=(), ttl_hours: float = 72.0, dry_run: bool = False) -> dict:
    """前缀级孤儿兜底：删掉「不在 `keep_rels` 且 COS 上最后修改早于 TTL」的对象。

    主清理路径是 `app.py::_sweep()`（本地删文件 → 同步删 COS）；本函数只兜底那些
    「本地已无、COS 仍留」的残留（上传成功但本地先被删、目录手工清理等）。

    防误删两条（与图案提取同规则）：
      ① `keep_rels` 为空且非 dry_run → **不执行**；
      ② 单次最多删 `MAX_PER_RUN` 个对象。
    """
    out = {"scanned": 0, "matched": 0, "deleted": 0, "bytes": 0, "keys": [],
           "dry_run": bool(dry_run), "skipped": ""}
    if not enabled():
        out["skipped"] = "COS 不可用"
        return out
    keep = set(key_for(r) for r in keep_rels if r)
    if not keep and not dry_run:
        out["skipped"] = "keep 为空，跳过（防误删）"
        if _S is not None:
            _S._log("wm " + out["skipped"])          # noqa: SLF001
        return out
    cutoff = time.time() - float(ttl_hours) * 3600.0
    doomed = []
    for it in list_all():
        out["scanned"] += 1
        if it["key"] in keep:
            continue
        mt = it.get("mtime")
        if mt is None or mt >= cutoff:               # 时间不明 / 未到期 → 保守跳过
            continue
        doomed.append((it["key"], it["size"]))
    out["matched"] = len(doomed)
    out["bytes"] = sum(s for _, s in doomed)
    out["keys"] = [k for k, _ in doomed][:200]
    if dry_run or not doomed:
        return out
    c, cfg = _S._get()                               # noqa: SLF001
    if c is None:
        return out
    for i in range(0, min(len(doomed), MAX_PER_RUN), 1000):
        batch = doomed[i:i + 1000]
        try:
            c.delete_objects(Bucket=cfg["bucket"],
                             Delete={"Object": [{"Key": k} for k, _ in batch], "Quiet": True})
            out["deleted"] += len(batch)
        except Exception as e:                       # noqa: BLE001
            _S._log("wm 孤儿清理批量删除失败: %s" % e)   # noqa: SLF001
    return out
