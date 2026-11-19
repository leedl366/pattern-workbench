# -*- coding: utf-8 -*-
"""PSD 兼容化工具（旧文件免重绘修复）——
把任意 PSD 里「图层通道 + 合并图像数据」的压缩方式统一转成 **RLE(PackBits)**。

为什么需要：
    Photoshop 对 PSD 的**合并图像数据段**只稳健支持 Raw(0) / RLE(1)。
    早期版本工作台默认用 ZIP+prediction(3)，部分 PS 版本打开时会直接报
    「无法打开，因为它与你的 Photoshop 版本不兼容」。
    本工具把 code 0/2/3 全部无损解压后按 RLE(1) 重编码，像素零损失。
    只重排压缩字节，**不改尺寸 / 图层 / 透明度 / 图层名 / 顺序 / dpi**。

用法：
    python psd_recompress.py <输入.psd> [更多.psd ...]
    python psd_recompress.py <输入.psd> --out 输出目录
    python psd_recompress.py <输入.psd> --suffix _PS兼容
    python psd_recompress.py <输入.psd> --force      # 已经是 RLE 也重新编码

默认输出到源文件同目录，文件名加后缀 `_PS兼容`。**永不覆盖源文件**。
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from image2psd import rle_channel  # noqa: E402  复用同一套 PackBits 编码器

CODEC = {0: "Raw", 1: "RLE(PackBits)", 2: "ZIP", 3: "ZIP+prediction"}
PS_OK = {0, 1}


# ---------------- 基础读取 ----------------
def _u16(b, o):
    return struct.unpack_from(">H", b, o)[0]


def _i16(b, o):
    return struct.unpack_from(">h", b, o)[0]


def _u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


# ---------------- PackBits 解码 ----------------
def _unpackbits(buf: bytes, expect: int) -> bytes:
    out = bytearray()
    i, n = 0, len(buf)
    while i < n:
        b = buf[i]
        i += 1
        if b < 128:                     # 字面量：b+1 字节
            c = b + 1
            out += buf[i:i + c]
            i += c
        elif b > 128:                   # 重复：257-b 次
            c = 257 - b
            if i >= n:
                break
            out += bytes([buf[i]]) * c
            i += 1
        # b == 128 → 空操作
        if len(out) >= expect:
            break
    return bytes(out[:expect])


def _unpredict_horizontal(raw: bytes, rows: int, width: int) -> bytes:
    """ZIP+prediction 的逆运算：每行内 data[i] += data[i-1]（行首保持原值）。"""
    a = np.frombuffer(raw, dtype=np.uint8, count=rows * width).reshape(rows, width)
    # uint32 累加后取低 8 位 == 逐元素 mod 256 前缀和
    return np.cumsum(a.astype(np.uint32), axis=1, dtype=np.uint32).astype(np.uint8).tobytes()


def _decode(blob: bytes, code: int, rows: int, width: int) -> bytes | None:
    """把一段通道数据解成裸平面（rows×width 字节）。失败返 None。"""
    expect = rows * width
    try:
        if code == 0:
            return blob[:expect] if len(blob) >= expect else None
        if code == 1:
            table = struct.unpack_from(">%dH" % rows, blob, 0)
            p = 2 * rows
            out = bytearray()
            for r in table:
                out += _unpackbits(blob[p:p + r], width)
                p += r
            return bytes(out) if len(out) >= expect else None
        if code == 2:
            raw = zlib.decompress(blob)
            return raw[:expect] if len(raw) >= expect else None
        if code == 3:
            raw = zlib.decompress(blob)
            if len(raw) < expect:
                return None
            return _unpredict_horizontal(raw, rows, width)
    except Exception:
        return None
    return None


def _encode(plane: np.ndarray) -> bytes:
    return rle_channel(plane)


# ---------------- PSD 解析 ----------------
def parse(d: bytes) -> dict:
    if d[:4] != b"8BPS":
        raise ValueError("不是 PSD/PSB 文件（文件头非 8BPS）")
    ver = _u16(d, 4)
    if ver != 1:
        raise ValueError("只支持 PSD（version=1），当前是 PSB（version=%d）" % ver)
    n_ch, h, w, depth, cm = struct.unpack_from(">HIIHH", d, 12)
    if depth != 8:
        raise ValueError("只支持 8bit/通道，当前 depth=%d" % depth)

    o = 26
    cm_len = _u32(d, o); o += 4 + cm_len
    ir_len = _u32(d, o); o += 4 + ir_len
    lmi_off = o
    lmi_len = _u32(d, o)
    lmi_start = o + 4
    merged_off = lmi_start + lmi_len

    layers = []
    if lmi_len:
        li_len = _u32(d, lmi_start)
        li_start = lmi_start + 4
        cnt = _i16(d, li_start)
        n = abs(cnt)
        p = li_start + 2
        for i in range(n):
            rec_start = p
            top, left, bot, right = struct.unpack_from(">iiii", d, p); p += 16
            nch = _u16(d, p); p += 2
            chans = []
            for _k in range(nch):
                len_off = p + 2
                cid = _i16(d, p)
                clen = _u32(d, p + 2)
                p += 6
                chans.append({"cid": cid, "len_off": len_off, "len": clen})
            p += 12                      # blend sig/key(8) + opacity/clip/flags/filler(4)
            extra = _u32(d, p); p += 4 + extra
            layers.append({"rec_start": rec_start, "rec_end": p, "chans": chans,
                           "h": bot - top, "w": right - left,
                           "rect": (top, left, bot, right)})
        for L in layers:
            for c in L["chans"]:
                c["data_off"] = p
                c["code"] = _u16(d, p) if c["len"] >= 2 else None
                p += c["len"]
        li_end = li_start + li_len
    else:
        li_end = lmi_start

    return dict(d=d, ver=ver, n_ch=n_ch, h=h, w=w, depth=depth, cm=cm,
                lmi_off=lmi_off, lmi_len=lmi_len, lmi_start=lmi_start,
                li_start=li_start if lmi_len else lmi_start, li_end=li_end,
                head_end=lmi_off, merged_off=merged_off, layers=layers)


# ---------------- 主转换 ----------------
def recompress(src: Path, dst: Path, force: bool = False) -> dict:
    d = src.read_bytes()
    info = parse(d)
    if info["merged_off"] + 2 > len(d):
        raise ValueError("合并图像数据段缺失，文件可能已损坏")

    report = {"src": str(src), "dst": str(dst), "layers": len(info["layers"]),
              "changed_layers": 0, "old_merged": None, "new_merged": None}

    # ① 图层通道：逐个独立解码 → RLE
    new_records = []
    chan_blocks = []
    for L in info["layers"]:
        rec = bytearray(d[L["rec_start"]:L["rec_end"]])
        rec_base = L["rec_start"]
        data_block = bytearray()
        for c in L["chans"]:
            if c["len"] < 2 or c["code"] is None:
                blob = d[c["data_off"]:c["data_off"] + c["len"]]
                data_block += blob
                continue
            raw_blob = d[c["data_off"] + 2:c["data_off"] + c["len"]]
            touched = False
            if c["code"] in PS_OK and not force:
                packed = raw_blob
                out_code = c["code"]
            else:
                plane = _decode(raw_blob, c["code"], L["h"], L["w"])
                if plane is None:
                    raise ValueError("图层 %d 通道 %d 解压失败（code=%s）"
                                     % (info["layers"].index(L), c["cid"],
                                        CODEC.get(c["code"], c["code"])))
                packed = _encode(np.frombuffer(plane, np.uint8).reshape(L["h"], L["w"]))
                out_code = 1
                touched = True
            if touched:
                report["changed_layers"] += 1
            data_block += struct.pack(">H", out_code) + packed
            # 回写通道记录里的长度字段
            rel = c["len_off"] - rec_base
            struct.pack_into(">I", rec, rel, 2 + len(packed))
        new_records.append(bytes(rec))
        chan_blocks.append(bytes(data_block))

    # ② 合并图像数据：所有通道行序列连成一体，一次编码
    m_code = _u16(d, info["merged_off"])
    report["old_merged"] = CODEC.get(m_code, m_code)
    m_blob = d[info["merged_off"] + 2:]
    rows_total = info["n_ch"] * info["h"]
    if m_code in PS_OK and not force:
        new_composite = struct.pack(">H", m_code) + m_blob
        report["new_merged"] = CODEC.get(m_code, m_code)
    else:
        raw = _decode(m_blob, m_code, rows_total, info["w"])
        if raw is None:
            raise ValueError("合并图像数据解压失败（code=%s）" % CODEC.get(m_code, m_code))
        plane = np.frombuffer(raw, np.uint8).reshape(rows_total, info["w"])
        new_composite = struct.pack(">H", 1) + _encode(plane)
        report["new_merged"] = "RLE(PackBits)"

    # ③ 重组 LayerAndMaskInfo（附加层信息 / 全局图层蒙版 / dpi 等原样搬过去）
    if info["lmi_len"]:
        layer_info = struct.pack(">h", len(info["layers"])) + b"".join(new_records) + b"".join(chan_blocks)
        if len(layer_info) % 2:
            layer_info += b"\x00"
        tail = d[info["li_end"]:info["merged_off"]]      # 全局蒙版 + 附加信息 + 对齐填充
        lmi_body = struct.pack(">I", len(layer_info)) + layer_info + tail
        new_lmi = struct.pack(">I", len(lmi_body)) + lmi_body
    else:
        new_lmi = struct.pack(">I", 0)

    out = bytes(d[:info["head_end"]]) + new_lmi + new_composite
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(out)
    report["src_bytes"] = len(d)
    report["dst_bytes"] = len(out)
    return report


def _default_dst(src: Path, out_dir: str | None, suffix: str) -> Path:
    stem = src.stem + suffix
    return (Path(out_dir) / (stem + src.suffix)) if out_dir else src.with_name(stem + src.suffix)


def main() -> int:
    ap = argparse.ArgumentParser(description="把 PSD 的通道压缩统一转成 RLE（修 PS 版本不兼容）")
    ap.add_argument("files", nargs="+", help="输入 .psd（可多个）")
    ap.add_argument("--out", default=None, help="输出目录（默认与源文件同目录）")
    ap.add_argument("--suffix", default="_PS兼容", help="输出文件名后缀（默认 _PS兼容）")
    ap.add_argument("--force", action="store_true", help="已是 RLE 也重新编码")
    ap.add_argument("--in-place", action="store_true",
                    help="[危险] 直接覆盖源文件（会自动备份为 .bak_原压缩）")
    args = ap.parse_args()

    bad = 0
    for f in args.files:
        src = Path(f)
        if not src.is_file():
            print("!! 找不到文件：%s" % src)
            bad += 1
            continue
        try:
            if args.in_place:
                bak = src.with_name(src.name + ".bak_原压缩")
                if not bak.exists():
                    bak.write_bytes(src.read_bytes())
                    print("   已备份原文件 → %s" % bak.name)
                dst = src
            else:
                dst = _default_dst(src, args.out, args.suffix)
                if dst.exists():
                    print("!! 已存在同名输出，跳过（避免覆盖）：%s" % dst)
                    bad += 1
                    continue
            r = recompress(src, dst, force=args.force)
            print("== %s" % src.name)
            print("   图层 %d 个（重编码通道 %d 个）" % (r["layers"], r["changed_layers"]))
            print("   合并图像数据：%s → %s" % (r["old_merged"], r["new_merged"]))
            print("   体积 %s → %s（%.2f MB → %.2f MB）"
                  % (r["src_bytes"], r["dst_bytes"], r["src_bytes"] / 1e6, r["dst_bytes"] / 1e6))
            if dst != src:
                print("   ✅ 输出：%s" % dst)
        except Exception as e:
            bad += 1
            print("!! %s 转换失败：%s" % (src.name, e))
    print()
    print("完成：成功 %d / 失败 %d" % (len(args.files) - bad, bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
