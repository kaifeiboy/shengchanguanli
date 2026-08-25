#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
clean_descriptive_qr.py - 清理切图中"描述性文件的二维码格式说明块"

判定规则（严格）：
  块被 ONNX 标为 engineering(image) 后仍会被 KeepViewBlock 直接保留，
  绕过二维码格式检测，导致这类"图纸说明示例块"漏网进匹配、产生图标假差异。

  应舍弃（描述性二维码格式块）：
    RawText 含以下任一描述性短语：
      "二维码标签激光打标格式" / "二维码格式" / "标签打印格式" /
      "此处激光打标二维码" / "二维码内容以实际生产为准" / "二维码标签打印格式"
    AND 不含真实打标区文字（LOW VOLTAGE / 禁止强电 / 地暖阀 / DC15/24V /
        服务热线 / 400 电话 / 型号标识）

  应保留（真实打标区说明块）：
    含上述真实打标文字的（如"下盖此处激光打标二维码 + LOWVOLTAGE/禁止强电/地暖阀"）

清理动作：
  1) DB: DELETE drawing_blocks WHERE DrawingId=? AND BIdx=?
  2) manifest: 移除对应 blocks 条目（保留空洞，idx 不重排）
  3) 磁盘: 删除 blocks/{did}_blk_{bidx:02d}.png

用法：
  --dry-run   仅报告不删除
  --did N     指定单个图纸
"""
import sys, os, json, sqlite3, argparse

DB = 'E:/workaaa/shengchanguanli/data/app.db'
SEG = 'E:/workaaa/shengchanguanli/data/seg'

# 描述性二维码格式短语（命中即疑似描述性说明块）
QR_DESC_PHRASES = [
    '二维码标签激光打标格式', '二维码标签打印格式', '二维码格式',
    '标签打印格式', '标签激光打标格式',
    '此处激光打标二维码', '二维码内容以实际生产为准',
    '此处激光打标二维',
]
# 真实打标区文字（命中即保留，哪怕也含二维码短语）。
# 注意：仅用「面板实际打标内容」词——LOW VOLTAGE/禁止强电/地暖阀/DC15/24V/服务热线/400电话。
# 不能用型号码(QHR/QHS/YCWA等)判断，因为"二维码格式说明块"里也有样品型号(QHRLA/QHRYK)，
# 那些是格式示例、非真实打标区。
REAL_MARKING = ['LOW VOLTAGE', 'LOWVOLTAGE', '禁止强电', '禁止塑电',
                '地暖阀', 'DC15/24V', 'DC15-24V', '服务热线',
                '400-', '4006', '4008', '400620']

def is_descriptive_qr(raw):
    if not raw:
        return False
    has_desc = any(p in raw for p in QR_DESC_PHRASES)
    if not has_desc:
        return False
    has_real = any(k.upper() in raw.upper() for k in REAL_MARKING)
    # 含真实打标文字 → 是真实打标区（下盖/上盖标签区），保留
    return not has_real


def collect():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT DrawingId,BIdx,RawText,Type FROM drawing_blocks ORDER BY DrawingId,BIdx'
    ).fetchall()
    conn.close()
    drop = []
    keep_ambiguous = []
    for r in rows:
        raw = r['RawText'] or ''
        did, bidx = r['DrawingId'], r['BIdx']
        if is_descriptive_qr(raw):
            drop.append((did, bidx, r['Type'], raw[:80]))
        elif any(p in raw for p in QR_DESC_PHRASES):
            # 含二维码短语但也被判为真实打标区 → 保留，记录供人工确认
            keep_ambiguous.append((did, bidx, raw[:80]))
    return drop, keep_ambiguous


def clean(drop_list, dry_run=True):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    removed = []
    for did, bidx, btype, rawprev in drop_list:
        # 1) manifest
        man_path = os.path.join(SEG, str(did), 'blocks', f'{did}_blocks.json')
        if os.path.exists(man_path):
            man = json.load(open(man_path, encoding='utf-8'))
            before = len(man['blocks'])
            man['blocks'] = [b for b in man['blocks'] if b['idx'] != bidx]
            after = len(man['blocks'])
            if not dry_run and before != after:
                json.dump(man, open(man_path, 'w', encoding='utf-8'),
                          ensure_ascii=False, indent=2)
        # 2) PNG
        png = os.path.join(SEG, str(did), 'blocks', f'{did}_blk_{bidx:02d}.png')
        png_exists = os.path.exists(png)
        # 3) DB
        conn.execute('DELETE FROM drawing_blocks WHERE DrawingId=? AND BIdx=?',
                     (did, bidx))
        if not dry_run:
            if png_exists:
                os.remove(png)
            conn.commit()
            removed.append((did, bidx))
        else:
            removed.append((did, bidx))  # dry-run 仅记录计划
    conn.close()
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--did', type=int, default=None)
    args = ap.parse_args()

    drop, keep_amb = collect()
    if args.did is not None:
        drop = [d for d in drop if d[0] == args.did]
        keep_amb = [d for d in keep_amb if d[0] == args.did]

    print('=' * 70)
    print(f'待清理（描述性二维码格式块）: {len(drop)} 个')
    print('=' * 70)
    for did, bidx, btype, raw in drop:
        print(f'  did={did:3d} blk={bidx:2d} type={btype:12s} raw={repr(raw)}')

    print()
    print('=' * 70)
    print(f'保留（含真实打标文字的二维码说明块，疑似误判）: {len(keep_amb)} 个')
    print('=' * 70)
    for did, bidx, raw in keep_amb:
        print(f'  did={did:3d} blk={bidx:2d} raw={repr(raw)}')

    if args.dry_run:
        print('\n[DRY-RUN] 未执行删除。去掉 --dry-run 执行清理。')
        return

    removed = clean(drop, dry_run=False)
    print(f'\n已清理 {len(removed)} 个块（DB + manifest + PNG）。')


if __name__ == '__main__':
    main()
