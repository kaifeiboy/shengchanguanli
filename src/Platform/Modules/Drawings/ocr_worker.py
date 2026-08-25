#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ocr_worker.py v1 — 常驻 OCR 工作进程（根治 .NET 反复拉起原生 Python 进程退化）
============================================================================

为什么需要它：
  .NET 宿主（长生命周期 ASP.NET 进程）在每次 /match-block（企微上传）与
  /segment（切图）请求时都 Process.Start 拉起一个全新 Python 进程。反复拉起
  原生进程会在「Python 解释器初始化 / 原生 DLL 加载 / 句柄耗尽」阶段偶发静默
  崩溃（exit=1、零 stdout/stderr）→ OCR 秒返空 → "无法识别"。这是"秒返"的真根因，
  重启宿主只是清脏，会再复发。

根治方案：
  - 宿主内【单例】惰性拉起「一个」ocr_worker.py 进程，RapidOCR 引擎在进程启动
    时【仅加载一次】，之后所有请求复用同一进程与同一引擎实例。
  - 经 stdin/stdout 行式 JSON 长连收图、返回文本；请求之间进程常驻，不再每次重拉。
  - 父进程侧做健康检查：worker 退出 / 请求超时 → 自动重启；彻底起不来 → 回退
    legacy「每次拉起」逻辑（永不丢失 OCR 能力）。

协议（行式 JSON，UTF-8，每条以 \\n 结尾并 flush）：
  启动成功后输出一行： {"__ready__": true}
  请求（父→worker, stdin）： {"id":"r1","image":"/abs/path.png","json":true}
  响应（worker→父, stdout）： {"id":"r1","text":"...","conf":0.9,"model_candidate":true,
                                   "engine":"rapidocr","error":null}

  - 单请求异常只写入 error 字段，绝不拖垮整个进程。
  - 父进程关闭 stdin（EOF）→ 主循环自然结束 → 干净退出（宿主重启无孤儿）。
  - 所有诊断日志走 stderr / ocr_crash.log，绝不污染 stdout 协议流。
"""

import sys, os, json, traceback

# 复用同目录 ocr_cli.py 的引擎与函数（避免重复 600+ 行预处理管线）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ocr_cli import (
    _get_rapidocr, _auto_roi, _run_rapidocr, _tesseract_fallback,
    extract_qr_codes, load_rgb_gray, is_model_candidate, norm_path,
)

_CRASH_LOG = r"E:\workaaa\shengchanguanli\data\ocr\ocr_crash.log"


def _log_crash(tag, exc):
    try:
        import datetime
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%H:%M:%S}] WORKER {tag}: {exc!r}\n")
            traceback.print_exc(file=f)
    except Exception:
        pass


def _ocr_image(inp):
    """对单张图做 OCR，返回 dict（等价 ocr_cli.main() 核心，但返回结构而非写 stdout）。"""
    import numpy as np
    engine_used = "rapidocr"
    roi_text = _auto_roi(inp)
    if roi_text and roi_text.strip():
        text, conf = roi_text, 0.9
        engine_used = "rapidocr_roi"
    else:
        text, conf, _ = _run_rapidocr(inp)

    fallback_used = False
    if not text.strip():
        text = _tesseract_fallback(inp)
        fallback_used = True

    # QR 增强提取
    try:
        _, gray = load_rgb_gray(inp)
        qr_texts = extract_qr_codes(gray.astype(np.uint8))
        for qt in qr_texts:
            text += f"\n[QR:]{qt}"
    except Exception:
        pass

    printed = text.split("\n[QR:]")[0].split("\n")[0] if text else ""
    mc = bool(is_model_candidate(printed))
    return {
        "text": text,
        "conf": round(float(conf), 1),
        "model_candidate": mc,
        "engine": "tesseract_fallback" if fallback_used else engine_used,
    }


def _combined_diff(img, req):
    """合并任务：在 worker 进程内跑 diff_visualizer.run_on_photo（RapidOCR 已加载→零重载）。

    请求字段：{mode:"combined", image, did, output, db, seg}
    返回：run_on_photo 的结果 dict（success / photoText / blocks / photo / error）。
    延迟导入 diff_visualizer，避免影响普通 OCR 启动路径。
    """
    did = str(req.get("did", "") or "")
    output = norm_path(str(req.get("output", "") or ""))
    db = norm_path(str(req.get("db", "") or "")) or None
    seg = norm_path(str(req.get("seg", "") or "")) or None
    if not did or not output:
        return {"success": False, "error": "combined 需要 did+output"}
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import diff_visualizer as dv
        _, res = dv.run_on_photo(
            img, did, output, db_path=db, seg_root=seg,
            max_out=1000, photo_ocr_max_dim=1280, enable_lcd_geocheck=True)
        return res
    except Exception as e:
        _log_crash("COMBINED", e)
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


def _main():
    # ---- Step 1: 启动加载引擎（仅一次）----
    try:
        _get_rapidocr()
    except Exception as e:
        # 不能写 stdout（会被父进程误判为 ready），仅留痕 stderr 后退出→父进程回退 legacy
        _log_crash("ENGINE_LOAD", e)
        sys.stderr.write(f"ENGINE LOAD FAILED: {e!r}\n")
        sys.stderr.flush()
        sys.exit(3)

    # ---- Step 1.5: Warm-up 推理（根治冷启动竞态）----
    #   仅 _get_rapidocr() 构造引擎对象【不够】：RapidOCR 的 ONNX session 在【第一次
    #   engine(img) 调用】时才真正加载模型权重。若不在启动时预热，头几次业务请求会
    #   触发懒加载 + 偶发异常 → 静默 fallback Tesseract（输出质量掉一截，且「忽好忽坏」）。
    #   这里强制跑一次真实推理，把模型权重真正加载好再宣告 ready。
    #   注意：warm-up 失败【不 fatal】——引擎对象已构造，真实图仍可能正常；fatal 反而
    #   会丢失 OCR 能力。失败仅留痕，由父进程侧 fallback 自愈兜底。
    warmup_ok = False
    try:
        import numpy as np, tempfile
        from PIL import Image
        dummy = (np.ones((32, 320, 3), dtype=np.uint8) * 255).copy()
        # 画几条黑线，让检测/识别都有真实输入（避免空图跳过某些模型分支）
        for y in (8, 16, 24):
            dummy[y:y + 3, 20:260] = 0
        tmp = os.path.join(tempfile.gettempdir(), "_ocr_warmup.png")
        Image.fromarray(dummy).save(tmp)
        try:
            _run_rapidocr(tmp)   # 触发 engine(img) → ONNX 模型真正加载
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass
        warmup_ok = True
    except Exception as e:
        _log_crash("WARMUP", e)   # 仅留痕；不 fatal

    # ---- Step 2: 宣告就绪（唯一一行非响应输出，附带引擎类型 + warmup 状态）----
    sys.stdout.write(json.dumps({
        "__ready__": True,
        "engine": "rapidocr",
        "warmup": warmup_ok,
    }, ensure_ascii=False) + "\n")
    sys.stdout.flush()

    # ---- Step 3: 常驻主循环 ----
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
            rid = str(req.get("id", ""))
            img = norm_path(str(req.get("image", "")))
        except Exception as e:
            _log_crash("BAD_REQUEST", e)
            continue  # 协议错误：跳过，不写任何响应（父进程会超时处理）

        resp = {
            "id": rid,
            "text": "",
            "conf": 0.0,
            "model_candidate": False,
            "engine": "",
            "error": None,
            "payload": None,
        }
        try:
            mode = str(req.get("mode", "") or "")
            if mode == "combined":
                # ⭐ 合并任务：在 worker 进程内直接跑 diff_visualizer.run_on_photo（RapidOCR 已加载→零重载），
                #    返回完整结果 dict（含 photoText / blocks / 标示图路径）。普通 OCR 路径不受影响。
                resp["payload"] = _combined_diff(img, req)
            elif not img or not os.path.exists(img):
                resp["error"] = "file_not_found"
            else:
                resp.update(_ocr_image(img))
        except Exception as e:
            resp["error"] = f"{type(e).__name__}: {e}"
            _log_crash("OCR_ONE", e)

        try:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception as e:
            _log_crash("WRITE_RESP", e)
            break  # stdout 已坏：退出，父进程将重启 worker


if __name__ == "__main__":
    try:
        _main()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as ex:
        _log_crash("FATAL", ex)
        sys.exit(1)
