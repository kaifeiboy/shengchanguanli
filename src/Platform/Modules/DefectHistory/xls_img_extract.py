# -*- coding: utf-8 -*-
"""从 .xls (BIFF8) 提取内嵌图片并定位到 (sheet, 数据行, 列)。

结构（实测 2026-08-06，WPS 生成的 BIFF8）：
  - 全局 MSODRAWINGGROUP(0xEB)+CONTINUE -> DggContainer(0xF000)
      -> Dgg(0xF006) + BstoreContainer(0xF001) -> BSE(0xF007)*N -> blip(PNG 0xF01E/JPEG 0xF01D/DIB 0xF01F)
  - 每 sheet MSODRAWING(0xEC)+CONTINUE -> DgContainer(0xF002)
      -> Dg(0xF008) + SpgrContainer(0xF003) -> {Spgr(0xF009), SpContainer(0xF004)*M}
      SpContainer -> {Sp(0xF00A, spid), FSP OPT(0xF00B, WPS 不写 pib), ClientAnchor(0xF010)}
  - ClientAnchor(18B): row1(2) col1(2) row2(2) col2(2) dx1(2) dy1(2) dx2(2) dy2(2) flag(2)
    图片所在数据行 = row1 - 2（R0 标题、R1 表头、R2 起数据）。

  关联：WPS 不写 FSP OPT 的 pib(0x07F8) 属性、OBJ 也不含 blip 引用 → 采用「位置映射」：
  图片形状按 (sheet 顺序, 形状顺序) 与全局 BStore blip 顺序一一对应；严格校验：
  总数相等 + 锚点 col==6（不良图片列）+ row 落在 [2, 数据区末行]。

用法：
  python xls_img_extract.py --file <path.xls> [--out <目录>] [--dump]
stdout JSON：{"success":true,"file":...,"totalBlips":N,"sheets":[{name,rowCount,nshapes,nobj,
             images:[{row,col,idx,fmt,len,file}]}],"warnings":[...]}
"""
import sys, os, json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import olefile

# ---------------- BIFF8 ----------------
REC_BOF = 0x0809
REC_EOF = 0x000A
REC_MSODRAWINGGROUP = 0x00EB
REC_MSODRAWING = 0x00EC
REC_CONTINUE = 0x003C
REC_OBJ = 0x005D
DT_WORKSHEET = 0x0010

# ---------------- OfficeArt（WPS 实测） ----------------
ART_DGG_CONTAINER = 0xF000
ART_BSTORE_CONTAINER = 0xF001
ART_DG_CONTAINER = 0xF002
ART_SPGR_CONTAINER = 0xF003
ART_SP_CONTAINER = 0xF004
ART_DGG = 0xF006
ART_BSE = 0xF007
ART_DG = 0xF008
ART_SPGR = 0xF009
ART_SP = 0xF00A
ART_OPT = 0xF00B
ART_CLIENT_ANCHOR = 0xF010
ART_CLIENT_DATA = 0xF011

BLIP_IMAGE_TYPES = {0xF01D: ('JPEG', '.jpg'), 0xF01E: ('PNG', '.png'),
                    0xF01F: ('DIB', '.dib'), 0xF029: ('TIFF', '.tif')}
IMG_COL = 6  # 不良图片列（0-based）


def walk_biff(data):
    i, n = 0, len(data)
    while i + 4 <= n:
        rt = int.from_bytes(data[i:i + 2], 'little')
        sz = int.from_bytes(data[i + 2:i + 4], 'little')
        yield rt, data[i + 4:i + 4 + sz], i
        i += 4 + sz


def walk_oa(data):
    """yield (rectype, recdata, abs_off, recVer, recInstance)"""
    i, n = 0, len(data)
    while i + 8 <= n:
        ver_inst = int.from_bytes(data[i:i + 2], 'little')
        rt = int.from_bytes(data[i + 2:i + 4], 'little')
        sz = int.from_bytes(data[i + 4:i + 8], 'little')
        out = (rt, data[i + 8:i + 8 + sz], i, ver_inst & 0x0F, ver_inst >> 12)
        i += 8 + sz
        yield out


def collect_stream(records, start_type):
    seg, started = [], False
    for rt, rec, hpos in records:
        if rt == start_type:
            started = True
            seg.append(rec)
        elif rt == REC_CONTINUE and started:
            seg.append(rec)
        elif started:
            break
    return b''.join(seg)


def sheet_names(path):
    try:
        import xlrd
        wb = xlrd.open_workbook(path, on_demand=True)
        return [ws.name for ws in wb.sheets()]
    except Exception:
        return None


def sheet_row_counts(path):
    """返回每 sheet 有效数据行数（跳过空行/合计行），供锚点行校验。"""
    try:
        import xlrd
        wb = xlrd.open_workbook(path)
        out = []
        for ws in wb.sheets():
            cnt = 0
            for r in range(2, ws.nrows):
                vals = [ws.cell_value(r, c) for c in range(min(ws.ncols, 8))]
                if any(str(v).strip() for v in vals):
                    cnt += 1
            out.append(cnt)
        return out
    except Exception:
        return None


def parse_opt_pib(rd):
    """FSP OPT：尝试 0/2 字节起始偏移，找 pid=0x07F8 简单属性。返回 pib 或 None。"""
    for off0 in (0, 2):
        i, n = off0, len(rd)
        while i + 6 <= n:
            opid = int.from_bytes(rd[i:i + 2], 'little')
            pid = opid & 0x0FFF
            is_complex = bool(opid & 0x4000)
            if pid == 0x07F8 and not is_complex:
                return int.from_bytes(rd[i + 2:i + 6], 'little')
            i += 6
    return None


def parse_client_anchor(rd):
    if len(rd) < 18:
        return None
    return int.from_bytes(rd[0:2], 'little'), int.from_bytes(rd[2:4], 'little')


class SpShape(object):
    __slots__ = ('spid', 'pib', 'anchor')

    def __init__(self):
        self.spid = None
        self.pib = None
        self.anchor = None


def parse_sp_container(rd, shapes):
    """递归解析任意容器（DgContainer/SpgrContainer/SpContainer），把形状追加到 shapes。"""
    for rt, srd, off, ver, inst in walk_oa(rd):
        if rt in (ART_SP_CONTAINER, ART_SPGR_CONTAINER, ART_DG_CONTAINER):
            parse_sp_container(srd, shapes)
        elif rt == ART_SP:
            sp = SpShape()
            if len(srd) >= 8:
                sp.spid = int.from_bytes(srd[4:8], 'little')
            shapes.append(sp)
        elif rt == ART_OPT and shapes:
            shapes[-1].pib = parse_opt_pib(srd)
        elif rt == ART_CLIENT_ANCHOR and shapes:
            a = parse_client_anchor(srd)
            if a:
                shapes[-1].anchor = a


def collect_sheet_msods(records):
    """按 sheet 分组收集全部 MSODRAWING 记录（WPS 每图一条，不拆 CONTINUE；
    若出现 CONTINUE 紧随其后则并入该条）。返回 [{name_index, objs, blobs:[...]}]。"""
    sheets, cur = [], None
    for rt, rec, hpos in records:
        if rt == REC_BOF and len(rec) >= 4 and int.from_bytes(rec[2:4], 'little') == DT_WORKSHEET:
            cur = {'objs': 0, 'blobs': []}
            sheets.append(cur)
        if cur is not None and rt == REC_EOF:
            cur = None
        if cur is not None:
            if rt == REC_MSODRAWING:
                cur['blobs'].append(bytearray(rec))
            elif rt == REC_CONTINUE and cur['blobs']:
                cur['blobs'][-1].extend(rec)
            elif rt == REC_OBJ:
                cur['objs'] += 1
    return sheets


IMG_MAGICS = (
    (b'\x89PNG\r\n\x1a\n', 'PNG'),
    (b'\xff\xd8\xff', 'JPEG'),
    (b'\xff\xd8\xff\xe0', 'JPEG'),
    (b'\xff\xd8\xff\xe1', 'JPEG'),
)


def extract_blip_bytes(bse_data):
    """从 BSE 记录数据中定位图片字节（魔数扫描 + PIL 校验）。
    BSE 头 36B：btWin32(1) btMacOS(1) rgbUid(16) tag(2) size(4) cRef(4) foDelay(4)
                usage(1) cbName(1) unused2(1) unused3(1)
    blip 区：blipHdr(8) + rgbUid(16) + tag(1) + 图片字节（WPS 的 recType 不可靠，以魔数为准）。
    返回 (fmt, bytes) 或 (None, None)。"""
    if len(bse_data) < 60:
        return None, None
    region = bse_data[36:]
    best = None
    for magic, fmt in IMG_MAGICS:
        idx = region.find(magic)
        if idx >= 0:
            cand = region[idx:]
            if best is None or idx < best[0]:
                best = (idx, fmt, cand)
    if best is not None:
        idx, fmt, cand = best
        # PIL 校验真实图片长度
        import io
        from PIL import Image
        for cut in (len(cand), len(cand) - 16, len(cand) - 8):
            try:
                im = Image.open(io.BytesIO(cand[:cut]))
                im.load()
                fmt2 = im.format or fmt
                return fmt2, cand[:cut]
            except Exception:
                continue
        return fmt, cand
    # DIB：BITMAPINFOHEADER(biSize=40) → 补 BITMAPFILEHEADER 成 BMP
    if region.startswith(b'\x28\x00\x00\x00'):
        import struct
        dib = region
        bmp = struct.pack('<2sIHHI', b'BM', 14 + len(dib), 0, 0, 14 + 40) + dib
        return 'BMP', bmp
    return None, None


def analyze_file(path, outdir=None):
    ole = olefile.OleFileIO(path)
    try:
        stream = None
        for s in ole.listdir():
            if s and s[-1].lower() in ('workbook', 'book'):
                stream = '/'.join(s)
                break
        if stream is None:
            return {"success": False, "error": "未找到 Workbook 流"}
        data = ole.openstream(stream).read()
    finally:
        ole.close()

    records = list(walk_biff(data))
    names = sheet_names(path)
    row_counts = sheet_row_counts(path)

    first_sheet_idx = None
    for idx, (rt, rec, hpos) in enumerate(records):
        if rt == REC_BOF and len(rec) >= 4 and int.from_bytes(rec[2:4], 'little') == DT_WORKSHEET:
            first_sheet_idx = idx
            break

    # 1) 全局 blips（顺序=BSE 序号）
    blips = []
    if first_sheet_idx is not None:
        blob = collect_stream(records[:first_sheet_idx], REC_MSODRAWINGGROUP)
        for rt, rd, off, ver, inst in walk_oa(blob):
            if rt != ART_DGG_CONTAINER:
                continue
            for rt2, rd2, off2, ver2, inst2 in walk_oa(rd):
                if rt2 != ART_BSTORE_CONTAINER:
                    continue
                for rt3, rd3, off3, ver3, inst3 in walk_oa(rd2):
                    if rt3 != ART_BSE:
                        continue
                    fmt, bdata = extract_blip_bytes(rd3)
                    if bdata:
                        blips.append((fmt, bdata))
                    else:
                        blips.append(('UNK', b''))
                break
            break

    # 2) 每 sheet 全部 MSODRAWING 记录（WPS 每图一条）+ OBJ
    sheets = collect_sheet_msods(records)

    # 3) 位置映射：所有带锚点的图片形状（文档顺序）↔ blips（BStore 顺序）
    doc_shapes = []  # (sheet_index, shape)
    for si, s in enumerate(sheets):
        for blob in s['blobs']:
            shapes = []
            parse_sp_container(bytes(blob), shapes)
            for sh in shapes:
                if sh.anchor is not None:
                    doc_shapes.append((si, sh))

    warnings = []
    if len(doc_shapes) != len(blips):
        warnings.append("图片形状数(%d) 与 blip 数(%d) 不一致，按 min 截断映射" % (len(doc_shapes), len(blips)))

    # 分配 blip 索引：有 pib 的用 pib；否则位置映射
    for n, (si, sh) in enumerate(doc_shapes):
        if sh.pib is not None and 1 <= sh.pib <= len(blips):
            pass
        else:
            sh.pib = n + 1  # 位置映射（1-based）

    out_sheets = []
    per_sheet_shapes = {}
    for si, sh in doc_shapes:
        per_sheet_shapes.setdefault(si, []).append(sh)
    for si, s in enumerate(sheets):
        shapes = per_sheet_shapes.get(si, [])
        images = []
        rc = row_counts[si] if row_counts and si < len(row_counts) else None
        nm = names[si] if names and si < len(names) else ('sheet%d' % si)
        for pos, sh in enumerate(shapes):
            if sh.anchor is None or sh.pib is None:
                continue
            bse_idx = sh.pib - 1
            if not (0 <= bse_idx < len(blips)):
                continue
            fmt, bdata = blips[bse_idx]
            if fmt not in ('PNG', 'JPEG', 'BMP'):
                continue
            # ⚠️ WPS 锚点 row1 恒为 2（不编码真实行）→ 行采用「位置映射」：第 pos 张 → 数据行 2+pos
            col1 = sh.anchor[1]
            row1 = 2 + pos
            drow = row1 - 2  # 数据行索引
            item = {'row': row1, 'col': col1, 'idx': sh.pib,
                    'spid': sh.spid, 'fmt': fmt, 'len': len(bdata)}
            if rc is not None:
                if not (2 <= row1 <= rc + 1):
                    warnings.append("%s: 位置映射行 %d 超出数据区(2..%d)" % (nm, row1, rc + 1))
            if col1 != IMG_COL:
                warnings.append("%s: 图片锚点列 %d 不是不良图片列(G=%d)，仍提取" % (nm, col1, IMG_COL))
            if outdir:
                ext = '.jpg' if fmt == 'JPEG' else ('.bmp' if fmt == 'BMP' else '.png')
                sdir = os.path.join(outdir, re_safe(nm))
                os.makedirs(sdir, exist_ok=True)
                fname = 'row_%d_col_%d_idx_%d%s' % (drow, col1, sh.pib, ext)
                fpath = os.path.join(sdir, fname)
                with open(fpath, 'wb') as f:
                    f.write(bdata)
                item['file'] = fpath
            images.append(item)
        out_sheets.append({
            'name': nm,
            'rowCount': rc,
            'nshapes': len(shapes),
            'nobj': s['objs'],
            'images': images,
        })
    return {"success": True, "file": os.path.basename(path), "totalBlips": len(blips),
            "sheets": out_sheets, "warnings": warnings}


def re_safe(s):
    import re
    return re.sub(r'[\\/:*?"<>|]', '_', s)


def main(argv):
    args = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith('--'):
            if '=' in a:
                k, v = a.split('=', 1)
                args[k[2:].replace('-', '_')] = v
            else:
                args[a[2:].replace('-', '_')] = argv[i + 1] if i + 1 < len(argv) else '1'
                i += 1
        i += 1
    fpath = args.get('file')
    if not fpath:
        print(json.dumps({"success": False, "error": "缺少 --file"}, ensure_ascii=False))
        return 1
    try:
        res = analyze_file(fpath, outdir=args.get('out'))
    except Exception as e:
        print(json.dumps({"success": False, "error": "解析失败: %s" % e}, ensure_ascii=False))
        return 1
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get('success') else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
