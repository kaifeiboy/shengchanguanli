#!/usr/bin/env python
# 本地 PDF -> 文字 提取器（供 .NET OcrService 调用）。
# 用 PyMuPDF 渲染每页为 PNG，再调本机 Tesseract(chi_sim+eng) OCR。
# 用法：python pdf_ocr.py "<pdf路径>"  -> 标准输出为拼接的 OCR 文本。
import sys, os, tempfile, subprocess

TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
MAX_PAGES = 8
DPI = 300

def ocr_pdf(pdf_path: str) -> str:
    import fitz  # PyMuPDF（已在受管 venv 安装，redist 装好后可用）
    doc = fitz.open(pdf_path)
    chunks = []
    for i, page in enumerate(doc):
        if i >= MAX_PAGES:
            break
        pix = page.get_pixmap(dpi=DPI)
        fd, png = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        pix.save(png)
        out_base = png[:-4]
        try:
            subprocess.run(
                [TESSERACT, png, out_base, "-l", "chi_sim+eng", "--psm", "6"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )
            txt_path = out_base + ".txt"
            if os.path.exists(txt_path):
                with open(txt_path, encoding="utf-8", errors="ignore") as f:
                    chunks.append(f.read())
                os.remove(txt_path)
        except Exception:
            pass
        finally:
            try: os.remove(png)
            except Exception: pass
    doc.close()
    return "\n".join(chunks)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("usage: pdf_ocr.py <pdf>\n")
        sys.exit(2)
    text = ocr_pdf(sys.argv[1])
    sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
    sys.stdout.write(text)
