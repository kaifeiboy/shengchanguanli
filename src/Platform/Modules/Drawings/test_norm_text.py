# -*- coding: utf-8 -*-
"""_norm_text 归一化单元测试 (L3 防回归) v19.13

抽离 diff_visualizer._norm_text 单独测试，避免加载 PIL 等重依赖。
覆盖：电话末尾不被截、型号 TS->15、NFC 双N折叠、中文不被误剥、短垃圾尾仍剥。
运行：python test_norm_text.py
"""
import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "diff_visualizer.py")


def _load_norm_text():
    src = open(SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_norm_text":
            code = compile(ast.Module(body=[node], type_ignores=[]), SRC, "exec")
            ns = {"re": re}
            exec(code, ns)
            return ns["_norm_text"]
    raise RuntimeError("_norm_text not found in diff_visualizer.py")


_norm_text = _load_norm_text()


def check(name, inp, expect_in):
    out = _norm_text(inp)
    ok = expect_in in out
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {inp!r} -> {out!r} (expect contains {expect_in!r})")
    assert ok, f"{name} FAILED: {out!r}"
    return out


def main():
    # 1. 电话末尾不被截（v19.13 核心修复）
    check("phone-dash", "服务热线：400-620-6607", "4006206607")   # 不再 4006206
    check("phone-did111", "服务热线：4008601111", "4008601111")  # 不再 4008601
    check("phone-did110", "服务热线：4006111111（转7）", "4006111111")
    check("phone-plain", "4006206607", "4006206607")             # 已干净不变
    # 2. 型号 TS->15 混淆校正
    check("model-ts", "YCWATSNCWQ", "ycwa15ncwq")
    # 3. NFC 双N折叠
    check("nfc-fold", "NNFC便捷控制", "nfc便捷控制")
    check("nfc-prefix-junk", "0净NFC便捷控制", "nfc便捷控制")
    # 4. 中文不被误剥（v19.10 修复保持）
    check("cjk-hotline", "服务热线", "服务热线")   # 不被剥成 务热线
    check("cjk-nostrong", "禁止强电", "禁止强电")  # 不被剥成 止强电
    # 5. 短垃圾尾仍剥（不破坏原有清理）
    check("junk-tail", "model88", "model")         # 88 短尾仍剥
    check("junk-tail2", "part00", "part")          # 00 短尾仍剥
    # 6. LCD 数字点号归一
    check("lcd-dot", "8.888", "8888")
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
