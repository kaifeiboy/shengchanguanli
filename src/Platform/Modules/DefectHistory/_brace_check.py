# -*- coding: utf-8 -*-
"""C# 括号平衡粗检（去字符串/注释后统计 {} 与 ()）。"""
import re, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

for f in sys.argv[1:]:
    s = open(f, encoding='utf-8').read()
    # 去掉字符串字面量
    s2 = re.sub(r'@"(?:[^"]|"")*"', '""', s)
    s2 = re.sub(r'"(?:[^"\\]|\\.)*"', '""', s2)
    # 去掉注释
    s2 = re.sub(r'//[^\n]*', '', s2)
    s2 = re.sub(r'/\*.*?\*/', '', s2, flags=re.S)
    ok = True
    for a, b in [('{', '}'), ('(', ')'), ('[', ']')]:
        ca, cb = s2.count(a), s2.count(b)
        st = 'OK' if ca == cb else 'MISMATCH'
        if ca != cb:
            ok = False
        print('%-28s %s=%d %s=%d %s' % (f, a, ca, b, cb, st))
    print('%-28s 行数=%d %s' % (f, len(s.splitlines()), 'OK' if ok else 'FAIL'))
