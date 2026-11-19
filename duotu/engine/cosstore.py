# -*- coding: utf-8 -*-
"""腾讯云 COS 对接（图片上传 / 删除 / 公网 URL）

设计要点（雪碧 2026-10-11 需求：整个工作台图片上传下载存放经过 COS）：
- 纯标准库（urllib/hashlib/hmac）手写 COS XML API 签名，服务器 venv 零新依赖；
- 配置存 data/cos_config.json（与 ai_config.json 同级，deploy.sh 不打包 data，线上凭证不会被覆盖）；
- 公有读私有写桶：上传/删除要签名，下载直接给公网 URL（无过期）；
- 所有操作失败只返回 None 并打日志，绝不抛异常——服务端自动回退 /api/file/ 本地地址。
"""
import os
import io
import json
import time
import hmac
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))            # .../工作台/engine
DATA = os.path.dirname(HERE)                                  # .../工作台
CONF_PATH = os.path.join(DATA, "data", "cos_config.json")

_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
         ".txt": "text/plain"}


# ---------------- 配置 ----------------
def load_conf():
    try:
        with open(CONF_PATH, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def save_conf(d):
    os.makedirs(os.path.dirname(CONF_PATH), exist_ok=True)
    with open(CONF_PATH, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)
    return d


def ok():
    """配置齐全且启用时才真干活"""
    c = load_conf()
    return bool(c.get("enabled") and c.get("bucket") and c.get("region")
                and c.get("secret_id") and c.get("secret_key"))


def _host(c):
    base = (c.get("base") or "").strip().rstrip("/")
    if base:
        return base[len("https://"):] if base.startswith("https://") else base
    return "{bucket}.cos.{region}.myqcloud.com".format(**c)


def url_for(kind, fn):
    """公有读直链：https://<bucket>.cos.<region>.myqcloud.com/<prefix>/<kind>/<fn>"""
    c = load_conf()
    if not (c.get("bucket") and c.get("region")):
        return None
    prefix = (c.get("prefix") or "").strip("/")
    key = "{0}/{1}".format(kind, fn)
    if prefix:
        key = prefix + "/" + key
    return "https://{0}/{1}".format(_host(c), key)


# ---------------- 签名（COS XML API）----------------
# 2026-10-11 修正：对照官方 SDK（qcloud_cos.cos_auth）——
#   ① 头部 value 必须按 quote(v, '-_.~') URL 编码（如 image/png 的 / 要编成 %2F），
#      否则真实 COS 返回 403 SignatureDoesNotMatch（mock 测不出，只有真桶能暴露）；
#   ② FormatString 的 URI 用原始未编码路径（SDK 同款），URL 编码只发生在实际请求地址上。
def _sign(c, method, key, headers, params=None):
    t = int(time.time())
    key_time = "{0};{1}".format(t - 60, t + 900)
    sign_key = hmac.new(c["secret_key"].encode("utf-8"), key_time.encode("utf-8"),
                        hashlib.sha1).hexdigest()
    q = lambda s: urllib.parse.quote(str(s), "-_.~")
    hl = sorted((q(str(k).lower()), q(str(v).strip())) for k, v in headers.items())
    h_names = ";".join(k for k, _ in hl)
    h_pairs = "&".join("{0}={1}".format(k, v) for k, v in hl)
    # 桶级子资源（?lifecycle 等）要参与签名：无值参数按 k= 签
    pl = sorted((q(str(k).lower()), q(str(v))) for k, v in (params or []))
    p_names = ";".join(k for k, _ in pl)
    p_pairs = "&".join("{0}={1}".format(k, v) for k, v in pl)
    http_str = "{0}\n/{1}\n{2}\n{3}\n".format(method.lower(), key.strip("/"),
                                              p_pairs, h_pairs)
    s2s = "sha1\n{0}\n{1}\n".format(key_time,
                                    hashlib.sha1(http_str.encode("utf-8")).hexdigest())
    sig = hmac.new(sign_key.encode("utf-8"), s2s.encode("utf-8"),
                   hashlib.sha1).hexdigest()
    return ("q-sign-algorithm=sha1&q-ak={0}&q-sign-time={1}&q-key-time={1}"
            "&q-header-list={2}&q-url-param-list={3}&q-signature={4}").format(
                c["secret_id"], key_time, h_names, p_names, sig)


def _req(c, method, key, body=None, ctype="application/octet-stream", timeout=30,
         params=None, bucket_level=False):
    host = _host(c)
    prefix = (c.get("prefix") or "").strip("/")
    # bucket_level=True 时是桶级子资源（?lifecycle 等）：URI 用 "/"，不拼 prefix
    full = "" if bucket_level else ("{0}/{1}".format(prefix, key) if prefix else key)
    # 签名用原始 key（SDK 同款）；实际 URL 只对非 ASCII/特殊字符编码，/ 保留
    url = "https://{0}/{1}".format(host, urllib.parse.quote(full, safe="/"))
    if params:
        url += "?" + "&".join("{0}={1}".format(k, v) for k, v in params)
    headers = {"Host": host}
    if body is not None:
        headers["Content-Type"] = ctype
    auth = _sign(c, method, full, headers, params=params)
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("Authorization", auth)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


# ---------------- 对外操作 ----------------
def put(local_path, kind, fn=None, timeout=60):
    """上传本地文件到 COS：key = <prefix>/<kind>/<文件名>。成功返回公网 URL，失败返回 None。"""
    if not ok():
        return None
    try:
        c = load_conf()
        fn = fn or os.path.basename(local_path)
        ext = os.path.splitext(fn)[1].lower()
        with open(local_path, "rb") as fh:
            body = fh.read()
        st, _ = _req(c, "PUT", "{0}/{1}".format(kind, fn), body,
                     ctype=_MIME.get(ext, "application/octet-stream"),
                     timeout=timeout)
        if 200 <= st < 300:
            return url_for(kind, fn)
        print("[cosstore] PUT %s/%s -> HTTP %s" % (kind, fn, st))
        return None
    except Exception as ex:
        print("[cosstore] PUT 失败 %s: %s" % (local_path, ex))
        return None


def delete(kind, fn, timeout=15):
    """删 COS 对象（删除/清空产出图时同步调用）。失败只打日志。"""
    if not ok():
        return False
    try:
        c = load_conf()
        st, _ = _req(c, "DELETE", "{0}/{1}".format(kind, fn), timeout=timeout)
        return 200 <= st < 300
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True          # 本来就没有，视为成功
        print("[cosstore] DELETE %s/%s -> HTTP %s" % (kind, fn, e.code))
        return False
    except Exception as ex:
        print("[cosstore] DELETE 失败 %s/%s: %s" % (kind, fn, ex))
        return False


# ---------------- 生命周期（7 天自动过期，2026-10-12 雪碧需求）----------------
def set_lifecycle(days=7):
    """设置桶生命周期规则：<prefix>/ 前缀的对象满 days 天由 COS 自动删除。
    COS 官方机制，规则设一次长期生效，服务端零负担。返回 (ok, msg)。"""
    if not ok():
        return False, "配置不完整（需要 region / bucket / SecretId / SecretKey）"
    c = load_conf()
    prefix = (c.get("prefix") or "").strip("/")
    filt = "<Prefix>{0}/</Prefix>".format(prefix) if prefix else "<Prefix/>"
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<LifecycleConfiguration><Rule>'
           '<ID>duotu-auto-expire</ID><Status>Enabled</Status>'
           '<Filter>{0}</Filter>'
           '<Expiration><Days>{1}</Days></Expiration>'
           '</Rule></LifecycleConfiguration>').format(filt, int(days))
    try:
        st, _ = _req(c, "PUT", "", body=xml.encode("utf-8"),
                     ctype="application/xml", params=[("lifecycle", "")], timeout=20,
                     bucket_level=True)
        if 200 <= st < 300:
            return True, "已设置：{0}/ 前缀对象 {1} 天自动删除".format(
                prefix or "(全桶)", int(days))
        return False, "PUT ?lifecycle -> HTTP {0}".format(st)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            detail = ""
        return False, "HTTP {0} {1}".format(e.code, detail)
    except Exception as ex:
        return False, "设置失败：{0}".format(ex)


def get_lifecycle():
    """读当前桶生命周期规则原文（验证用）。返回 (ok, xml或错误)。"""
    if not ok():
        return False, "配置不完整"
    c = load_conf()
    try:
        st, body = _req(c, "GET", "", params=[("lifecycle", "")], timeout=20,
                        bucket_level=True)
        return True, body.decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True, "(未设置任何生命周期规则)"
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            detail = ""
        return False, "HTTP {0} {1}".format(e.code, detail)
    except Exception as ex:
        return False, "读取失败：{0}".format(ex)


def probe():
    """凭证验证：传一个小探针文件再删掉。返回 (ok, msg)。"""
    if not ok():
        return False, "配置不完整（需要 region / bucket / SecretId / SecretKey）"
    c = load_conf()
    key = "_probe_{0}.txt".format(int(time.time()))
    try:
        st, _ = _req(c, "PUT", key, b"duotu probe", ctype="text/plain", timeout=15)
        if not (200 <= st < 300):
            return False, "上传探针失败 HTTP %s" % st
        # 探针 key 不带 kind 前缀，直接用 _req DELETE 清掉
        _req(c, "DELETE", key, timeout=15)
        return True, "凭证有效（bucket: {0}, region: {1}）".format(
            c.get("bucket"), c.get("region"))
    except urllib.error.HTTPError as e:
        msg = "HTTP %s" % e.code
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
            msg += " " + detail
        except Exception:
            pass
        if e.code in (401, 403):
            return False, "签名/权限被拒（%s）：检查 SecretId/SecretKey 与桶权限" % msg
        return False, "连接失败（%s）" % msg
    except Exception as ex:
        return False, "连接失败：{0}".format(ex)
