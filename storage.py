# -*- coding: utf-8 -*-
"""COS 对象存储：图案提取工作台产物统一上传腾讯云 COS。

设计原则（最小影响、可回退）：
- **双写兜底**：本地仍存一份（`data/<tid>/`，作 CDN 兜底与缩略图源）；COS 上传成功则对外给
  COS 公网 URL；COS 未配置 / 上传失败 → 返回 None，前端回退 `/api/tasks/<tid>/file/xxx`，
  行为与改造前完全一致。
- **前缀隔离**：图案工作台产物统一放 `pattern/` 前缀（与银饰 `yinshi/`、闲鱼 `xianyu/` 同桶
  不同目录，互不干扰）。
  前缀可用 `PATTERN_COS_PREFIX` 环境变量或 `cos_config.json` 的 `"prefix"` 字段覆盖
  （本地开发实例设为 `pattern_dev/`，见下方 `PREFIX` 注释里的隔离铁律）。
- **密钥不落代码库顶层**：读 `<app>/cos_config.json`（与 /opt/xianyu-backend 同格式）。

对外 API：
    enabled()                      -> COS 是否可用
    public_url(key)                -> key 的公网 URL
    remote_url(tid, rel)           -> 该任务某个相对路径产物的公网 URL（不校验存在性）
    upload_file(local, key)        -> 上传，成功返回公网 URL，失败 None
    upload_bytes(raw, key, ctype)  -> 同上（内存字节）
    upload_task_files(tid, base, rels) -> 批量上传某任务产物，返回 {rel: url}
    delete_task(tid)               -> 删任务时清掉 COS 上该前缀（尽力而为）
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.environ.get("PATTERN_COS_CFG", os.path.join(BASE, "cos_config.json"))

def _norm_prefix(p) -> str:
    """"pattern_dev" / "/pattern_dev/" / "pattern_dev/" → 统一为 "pattern_dev/"；空 → "pattern/"。"""
    p = str(p or "").strip().strip("/")
    return (p + "/") if p else "pattern/"


def _cfg_prefix() -> str:
    """cos_config.json 里的可选 `prefix` 字段（本地隔离用；线上不写）。读不到返回 ""。"""
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            return _norm_prefix(json.load(f).get("prefix"))
    except Exception:                                   # noqa: BLE001
        return ""


# 图案工作台在桶内的顶层前缀。
#
# 🔴 隔离铁律：本地开发实例与线上**共用同一个桶**。若两边都用 `pattern/`，本地一启动
# `_backfill_cdn` 就会把本地 `data/` 的全部历史任务灌上 COS；线上 `data/` 里没有这些
# 目录 → 线上看全是孤儿，且**每次本地启动都会重灌一遍**（2026-10-11 实测：本地 12 个
# 历史任务 / 268MB 反复回灌）。
#
# 优先级：`PATTERN_COS_PREFIX` 环境变量 > `cos_config.json` 的 `"prefix"` > `"pattern/"`。
# 线上不设任何一项 → 天然落在 `pattern/`；本地在 `cos_config.json` 写 `"prefix":
# "pattern_dev/"` → 任何启动方式都自动隔绝，无需记环境变量。
PREFIX = _norm_prefix(os.environ.get("PATTERN_COS_PREFIX") or _cfg_prefix())

# 🔴 任务级清扫的豁免子前缀（2026-10-15）。
#
# `sweep_prefix()` 用 `key.split("/")[1]` 当 tid 判「是不是孤儿」，但 `pattern/` 下并非
# 只有 `pattern/<tid>/...` 一种布局：
#   pattern/trim/<hash>.png  ← `/api/trim` 的公共裁切缓存（与任何 tid 无关）
# 它的 `parts[1] == "trim"` 永远不在 `keep_tids` 里 → 到期即被当孤儿删掉，
# 导致素材库对比图反复重算（每次都要重新下载原图 + 找白边 + 重新推 COS）。
# 这类「不挂在任务命名空间下」的子前缀一律跳过，由各自模块自己的 TTL 逻辑管理。
SWEEP_KEEP_SUB = ("trim",)

_CTYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".psd": "image/vnd.adobe.photoshop",
    ".json": "application/json",
}

def _log(msg):
    """ASCII 安全的日志输出（Windows 控制台编码不炸线程）。"""
    try:
        print(("[storage] " + str(msg)).encode("ascii", "replace").decode("ascii"), flush=True)
    except Exception:
        pass

_client = None
_cfg = None
_tried = False
_public_base = None


def _get():
    """惰性初始化 COS 客户端；不可用则返回 (None, None)，不抛异常。"""
    global _client, _cfg, _tried, _public_base
    if _client is not None:
        return _client, _cfg
    if _tried:
        return None, None
    _tried = True
    try:
        from qcloud_cos import CosConfig, CosS3Client
        cfg = json.load(open(CFG_PATH, encoding="utf-8"))
        # 铁律：必须显式设 Timeout。SDK 的 `send_request` 会用 `_conf._timeout` 无条件覆盖
        # 调用方 timeout（cos_client.py:405），而分片上传单片默认 **1200 秒**（cos_client.py:1167）。
        # 跨地域公网（本机 ap-shanghai → 桶 ap-guangzhou）偶发连接停顿时，未设超时会让一次
        # upload 静默挂住最多 20 分钟（×3 重试），把出图/回填线程整条拖死。设 90 秒：
        # 单片 1MB 实测约 3~4 秒，余量充足；真抖动则快速失败 → 返回 None → 前端回退本地。
        conf = CosConfig(Region=cfg["region"], SecretId=cfg["secret_id"],
                         SecretKey=cfg["secret_key"], Scheme="https",
                         Timeout=int(cfg.get("timeout", 90)))
        _client = CosS3Client(conf)
        _cfg = cfg
        # 可选：自定义 CDN 加速域名（cos_config.json 里加 "domain": "https://cdn.xxx.com"）
        dom = (cfg.get("domain") or "").rstrip("/")
        _public_base = (dom + "/") if dom else (
            "https://%s.cos.%s.myqcloud.com/" % (cfg["bucket"], cfg["region"]))
        _log("COS 就绪 bucket=%s region=%s prefix=%s base=%s"
              % (cfg["bucket"], cfg["region"], PREFIX, _public_base))
        return _client, cfg
    except Exception as e:  # noqa: BLE001
        _log("COS 不可用，回退本地文件服务：%s" % e)
        return None, None


def enabled() -> bool:
    c, _ = _get()
    return c is not None


def public_url(key: str) -> str:
    return (_public_base or "") + urllib.parse.quote(key, safe="/")


def key_for(tid: str, rel: str) -> str:
    """`data/<tid>/parts/图案_支架.png` → `pattern/<tid>/parts/图案_支架.png`"""
    return PREFIX + str(tid) + "/" + str(rel).replace("\\", "/").lstrip("/")


def remote_url(tid: str, rel: str) -> "str | None":
    """不校验存在性，直接给出该产物在 COS 上的公网 URL（COS 不可用返回 None）。"""
    c, _ = _get()
    if c is None:
        return None
    return public_url(key_for(tid, rel))


def _ctype_of(path: str) -> str:
    return _CTYPE.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def upload_file(local_path: str, key: str, attempts: int = 2) -> "str | None":
    """上传本地文件到 COS，返回公网 URL；失败返回 None（不抛，调用方回退）。

    超时/抖动最多重试 `attempts` 次（客户端已在 `_get()` 里设了 90 秒硬超时，
    所以最坏等待有上界，绝不会再出现「无限挂起」）。
    """
    c, cfg = _get()
    if c is None:
        return None
    last = None
    for i in range(max(1, attempts)):
        try:
            c.upload_file(Bucket=cfg["bucket"], Key=key, LocalFilePath=local_path,
                          EnableMD5=False, ContentType=_ctype_of(local_path))
            return public_url(key)
        except Exception as e:  # noqa: BLE001
            last = e
            if i + 1 < max(1, attempts):
                time.sleep(1.5)
    _log("上传失败 %s: %s" % (key, last))
    return None


def upload_bytes(raw: bytes, key: str, content_type: str = "application/octet-stream") -> "str | None":
    c, cfg = _get()
    if c is None:
        return None
    try:
        c.put_object(Bucket=cfg["bucket"], Body=raw, Key=key, ContentType=content_type)
        return public_url(key)
    except Exception as e:  # noqa: BLE001
        _log("上传(bytes)失败 %s: %s" % (key, e))
        return None


def upload_task_files(tid: str, base_dir: str, rels, on_log=None) -> dict:
    """把某任务下的若干相对路径产物批量上传 COS。

    返回 {rel: 公网URL}（失败的 rel 不会出现在结果里，前端自然回退本地）。
    """
    out = {}
    c, _ = _get()
    if c is None:
        return out
    for rel in rels:
        rel = str(rel).replace("\\", "/")
        fp = os.path.join(base_dir, rel.replace("/", os.sep))
        if not os.path.isfile(fp):
            continue
        url = upload_file(fp, key_for(tid, rel))
        if url:
            out[rel] = url
            if on_log:
                on_log("已上传 CDN：%s" % rel)
    return out


def _lm_epoch(s):
    """COS 的 LastModified（`2026-10-11T03:31:40.000Z`，UTC）→ epoch 秒。解析失败 None。"""
    s = str(s or "").strip()
    if not s:
        return None
    import calendar
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return float(calendar.timegm(time.strptime(s, fmt)))
        except Exception:                               # noqa: BLE001
            continue
    return None


def list_all(prefix: str = PREFIX):
    """列出前缀下全部对象 → [{"key","size","mtime"}]（尽力而为，失败返回 []）。"""
    c, cfg = _get()
    out = []
    if c is None:
        return out
    marker = ""
    try:
        while True:
            resp = c.list_objects(Bucket=cfg["bucket"], Prefix=prefix, Marker=marker, MaxKeys=1000)
            contents = resp.get("Contents") or []
            if not isinstance(contents, list):
                contents = [contents]
            if not contents:
                break
            for it in contents:
                k = it.get("Key")
                if not k:
                    continue
                out.append({"key": k, "size": int(it.get("Size") or 0),
                            "mtime": _lm_epoch(it.get("LastModified"))})
            if resp.get("IsTruncated") == "true":
                marker = resp.get("NextMarker") or contents[-1]["Key"]
            else:
                break
    except Exception as e:                              # noqa: BLE001
        _log("列举对象失败 %s: %s" % (prefix, e))
    return out


def sweep_prefix(ttl_hours: float = 72.0, keep_tids=(), dry_run: bool = False,
                 prefix: str = PREFIX) -> dict:
    """清理 `pattern/` 下「最后修改时间早于 ttl_hours」且所属 tid 不在 `keep_tids` 的对象。

    这是任务级清理的**兜底**：能清掉「任务记录已不在 / 从未登记」的孤儿
    ——测试残留（`_t_*`）、删除失败的历史任务、上传成功但任务目录丢了的对象。

    两条防误删：
      ① `keep_tids` 为空时**不执行**（不 dry_run），避免 worker 异常导致空表大清洗；
      ② 单次调用最多删 `MAX_PER_RUN` 个对象，防止一次失控清空整个前缀。

    返回 {"scanned","matched","deleted","bytes","keys"}（keys 为前 200 个待删 key）。
    """
    MAX_PER_RUN = 2000
    c, cfg = _get()
    out = {"scanned": 0, "matched": 0, "deleted": 0, "bytes": 0, "keys": [],
           "dry_run": bool(dry_run), "skipped": ""}
    if c is None:
        return out
    keep = set(str(x) for x in keep_tids)
    if not keep and not dry_run:
        out["skipped"] = "keep 为空，跳过（防误删）"
        _log(out["skipped"])
        return out
    cutoff = time.time() - float(ttl_hours) * 3600.0
    doomed = []
    for it in list_all(prefix):
        out["scanned"] += 1
        parts = str(it["key"]).split("/")
        tid = parts[1] if len(parts) > 2 else ""
        if tid in SWEEP_KEEP_SUB:           # 非任务命名空间的公共缓存 → 豁免
            continue
        if tid in keep:
            continue
        t = it.get("mtime")
        if t is None or t >= cutoff:        # 时间不明 / 未到期 → 保守跳过
            continue
        doomed.append((it["key"], it["size"]))
    out["matched"] = len(doomed)
    out["bytes"] = sum(s for _, s in doomed)
    out["keys"] = [k for k, _ in doomed][:200]
    if dry_run or not doomed:
        return out
    for i in range(0, min(len(doomed), MAX_PER_RUN), 1000):
        batch = doomed[i:i + 1000]
        try:
            c.delete_objects(Bucket=cfg["bucket"],
                             Delete={"Object": [{"Key": k} for k, _ in batch], "Quiet": True})
            out["deleted"] += len(batch)
        except Exception as e:                          # noqa: BLE001
            _log("批量删除失败: %s" % e)
    return out


def delete_prefix(prefix: str) -> int:
    """删除任意前缀下的全部对象（分页批量删）。尽力而为，失败不抛，返回删除个数。

    通用入口：图案提取走 `delete_task(tid)`（= PREFIX + tid + "/"）；
    去水印版块（`wm/storage.py`）走本函数删自己的 `wm/` 前缀。
    """
    c, cfg = _get()
    if c is None:
        return 0
    n = 0
    try:
        marker = ""
        while True:
            resp = c.list_objects(Bucket=cfg["bucket"], Prefix=prefix, Marker=marker, MaxKeys=1000)
            contents = resp.get("Contents") or []
            if not contents:
                break
            objs = [{"Key": it["Key"]} for it in contents]
            c.delete_objects(Bucket=cfg["bucket"], Delete={"Object": objs, "Quiet": True})
            n += len(objs)
            if resp.get("IsTruncated") == "true":
                marker = resp.get("NextMarker") or contents[-1]["Key"]
            else:
                break
    except Exception as e:  # noqa: BLE001
        _log("删除 COS 前缀失败 %s: %s" % (prefix, e))
    return n


def delete_task(tid: str) -> int:
    """删除 COS 上该任务前缀下的全部对象（任务被删除时调用）。尽力而为，失败不抛。"""
    return delete_prefix(PREFIX + str(tid) + "/")
