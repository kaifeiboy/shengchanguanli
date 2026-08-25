# -*- coding: utf-8 -*-
"""调试：hexdump sheet MSODRAWING 全量 + 全局 BSE(0xF007) 头部，人工核对布局。"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import olefile
import xls_img_extract as X

path = sys.argv[1] if len(sys.argv) > 1 else r'E:\workaaa\shengchanguanli\data\defecthistory_test\2025年11月份IPQC及QA发现检验问题点.xls'
ole = olefile.OleFileIO(path)
data = ole.openstream('Workbook').read()
ole.close()
records = list(X.walk_biff(data))

first_sheet_idx = None
for idx, (rt, rec, hpos) in enumerate(records):
    if rt == X.REC_BOF and len(rec) >= 4 and int.from_bytes(rec[2:4], 'little') == X.DT_WORKSHEET:
        first_sheet_idx = idx
        break

# 1) sheet0 MSODRAWING 全量 hex
seg, started = [], False
for rt, rec, hpos in records[:first_sheet_idx]:
    if rt == X.REC_MSODRAWINGGROUP:
        started = True; seg.append(rec)
    elif rt == X.REC_CONTINUE and started:
        seg.append(rec)
    elif started:
        break
glob_blob = b''.join(seg)

# per-sheet msodrawing of sheet 0
msod_seg = []
for rt, rec, hpos in records:
    if rt == X.REC_MSODRAWING:
        msod_seg = [rec]
    elif rt == X.REC_CONTINUE and msod_seg:
        msod_seg.append(rec)
    elif msod_seg and rt not in (X.REC_CONTINUE,):
        break
msod = b''.join(msod_seg)
print('=== sheet0 MSODRAWING len', len(msod), '===')
print(msod.hex())

# 2) BSE(0xF007) 头部 96 字节
def walk_oa(data):
    out=[]; i=0; n=len(data)
    while i+8<=n:
        ver_inst=int.from_bytes(data[i:i+2],'little')
        rt=int.from_bytes(data[i+2:i+4],'little')
        sz=int.from_bytes(data[i+4:i+8],'little')
        out.append((rt, data[i+8:i+8+sz], i, ver_inst&0x0F, ver_inst>>12))
        i+=8+sz
    return out

for rt, rd, off, ver, inst in walk_oa(glob_blob):
    if rt == 0xF000:
        for rt2, rd2, off2, ver2, inst2 in walk_oa(rd):
            if rt2 == 0xF001:
                for rt3, rd3, off3, ver3, inst3 in walk_oa(rd2):
                    print('=== BSE/0x%04x len %d off %d ver %d inst %d ===' % (rt3, len(rd3), off3, ver3, inst3))
                    print('head96:', rd3[:96].hex())
