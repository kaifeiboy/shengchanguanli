#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量删除误删修复：恢复被误删的 8-10 有图数据 + 倒序删除 5 月无图重复数据。
用法: python -E _fix_batchdel.py [root 默认 E:\\生产不良履历]
前置: 关闭 WPS 中打开的不良履历汇总.xlsx（否则写操作被锁 WinError 5）。
"""
import os
import sys
import io
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import defect_history as dh


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else r"E:\生产不良履历"
    book = dh.book_path(root)
    # 1) 锁检测
    if os.path.exists(os.path.join(root, "~$不良履历汇总.xlsx")):
        print("⚠️ WPS 正打开着汇总表（存在 ~$ 锁文件），请先关闭再运行")
        sys.exit(2)
    try:
        with open(book, "r+b"):
            pass
    except PermissionError:
        print("⚠️ 文件被锁定，请先关闭 WPS 中的 不良履历汇总.xlsx")
        sys.exit(2)

    # 2) 恢复被误删的 8-10 数据（从 .bak 提取的图片）
    img = glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "temp", "_restore_img_*.png"))
    # temp 可能不在该相对路径，尝试常见位置
    if not img:
        cands = [
            r"E:\workaaa\shengchanguanli\temp\_restore_img_0.png",
        ]
        img = [c for c in cands if os.path.exists(c)]
    if not img:
        print("⚠️ 未找到恢复图片（_restore_img_0.png），跳过 8-10 恢复")
    else:
        q8 = dh.cmd_query({"root": root, "month": "2026-08"})
        exists = any(x["model"] == "1540" and x["hasImage"] for x in q8.get("rows", []))
        if exists:
            print("8-10 '1线' 1540 有图数据已存在，跳过恢复")
        else:
            r = dh.cmd_add_single({
                "root": root, "date": "2026.8.10", "line": "1线", "model": "1540",
                "reason": "红黑位置，胶不能溢出板的焊点面", "qty": "", "stage": "DIP",
                "handle": "作业不良", "images": img[0],
            })
            print("恢复 8-10 1540:", r.get("ok"), "| images:", r.get("images"))

    # 3) 倒序删除 5 月无图数据（历史遗留重复）
    q5 = dh.cmd_query({"root": root, "month": "2026-05"})
    noimg = [x["row"] for x in q5.get("rows", []) if not x["hasImage"]]
    print("5 月无图行(row):", noimg)
    for row in sorted(noimg, reverse=True):   # 倒序删除，避免索引位移
        r = dh.cmd_delete_row({"root": root, "row": row})
        print("  删 row=%d → %s" % (row, r.get("ok")))

    # 4) 验证
    q5 = dh.cmd_query({"root": root, "month": "2026-05"})
    q8 = dh.cmd_query({"root": root, "month": "2026-08"})
    m = dh.cmd_meta({"root": root})
    ic = dh.in_cell_images(dh.book_path(root), dh.SHEET_NAME)
    print()
    print("=== 验证 ===")
    print("5 月:", [(x["date"], x["model"], x["hasImage"]) for x in q5.get("rows", [])])
    print("8 月:", [(x["date"], x["line"], x["model"], x["hasImage"]) for x in q8.get("rows", [])])
    print("totalRows:", m.get("totalRows"), "| in-cell 图:", len(ic))
    qa = dh.cmd_query({"root": root})
    print("全部数据行:", qa.get("total"))


if __name__ == "__main__":
    main()
