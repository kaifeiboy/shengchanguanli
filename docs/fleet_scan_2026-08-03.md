# 全量 22 图 selfblock 扫描报告（v19.37, 2026-08-03）

## 扫描参数

- script: `E:\workaaa\shengchanguanli\src\Platform\Modules\Drawings\diff_visualizer.py` (v19.37)
- mode: selfblock（photo = block = CAD 切块，纯规则内部一致性测试）
- input override: DB RawText + Icons(v19.37 truthy 检查)
- 范围: did 106 ~ 132, 共 22 图, 99 块

## 汇总统计

| 指标 | 值 | 说明 |
|---|---|---|
| 总块数 | 99 | selfblock 模式 |
| R（缺标）| 15 | ❌ 有假阳 |
| Y（多标）| 10 | ❌ 有假阳 |
| G（一致）| 577 | |
| Gray | 611 | |
| OOB Y 块 | 0 | ✅ |
| OOB R 块 | 0 | ✅ |
| 错误 | 0 | |

## 问题块清单（按 did 排序）

```
  did=106 blk=0 blocksize=428×190 R2
  did=106 blk=1 blocksize=202×484 R1 Y1
  did=106 blk=3 blocksize=633×418 Im1
  did=106 blk=4 blocksize=408×190 R1
  did=107 blk=0 blocksize=536×698 R1 Y1
  did=107 blk=3 blocksize=453×398 Im1
  did=108 blk=2 blocksize=184×530 Im1
  did=108 blk=4 blocksize=433×394 Im1
  did=109 blk=0 blocksize=523×272 Im2
  did=109 blk=2 blocksize=183×546 Im1
  did=110 blk=0 blocksize=734×295 Im1
  did=110 blk=3 blocksize=639×404 R1 Y1 Im1
  did=111 blk=0 blocksize=734×298 Im1
  did=111 blk=3 blocksize=637×402 R1 Y1 Im1
  did=112 blk=0 blocksize=540×275 Im2
  did=112 blk=1 blocksize=441×459 Im10
  did=113 blk=0 blocksize=537×682 R1 Y1
  did=114 blk=0 blocksize=537×696 R1 Y1
  did=114 blk=2 blocksize=587×477 Im1
  did=114 blk=4 blocksize=492×299 Im1
  did=115 blk=3 blocksize=388×399 Im1
  did=116 blk=2 blocksize=478×461 R1 Y1
  did=116 blk=3 blocksize=391×400 Im1
  did=116 blk=4 blocksize=462×261 Ie1
  did=117 blk=4 blocksize=434×288 Im1
  did=117 blk=5 blocksize=418×279 Im1
  did=118 blk=0 blocksize=1164×381 Im1
  did=118 blk=2 blocksize=1076×458 Im1
  did=119 blk=0 blocksize=408×190 R2
  did=121 blk=1 blocksize=650×946 R1 Y1
  did=123 blk=0 blocksize=978×284 Im1
  did=123 blk=2 blocksize=484×564 Im1
  did=123 blk=3 blocksize=552×480 Im1
  did=124 blk=0 blocksize=549×234 Im1
  did=124 blk=1 blocksize=595×504 R1 Y1 Im2
  did=124 blk=4 blocksize=468×387 Im1
  did=126 blk=1 blocksize=281×509 R1 Y1
  did=126 blk=4 blocksize=587×270 Ie1
  did=132 blk=4 blocksize=434×288 Im1
  did=132 blk=5 blocksize=418×279 Im1
```
