import sys, os, faulthandler
faulthandler.enable()
sys.stderr.write("env FLAGS_use_mkldnn=%s DNNL_MAX_CPU_ISA=%s OMP=%s\n" % (
    os.environ.get("FLAGS_use_mkldnn"), os.environ.get("DNNL_MAX_CPU_ISA"), os.environ.get("OMP_NUM_THREADS")))
sys.stderr.flush()
try:
    import paddle
    sys.stderr.write("paddle imported %s\n" % paddle.__version__); sys.stderr.flush()
    from paddleocr import LayoutDetection
    sys.stderr.write("creating V3 (mkldnn off)...\n"); sys.stderr.flush()
    m = LayoutDetection(model_name="PP-DocLayoutV3", device="cpu")
    sys.stderr.write("V3 CREATED OK\n"); sys.stderr.flush()
    res = m.predict(r"E:/workaaa/shengchanguanli/data/seg/_v3final_hsxc/page.png")
    sys.stderr.write("PREDICT OK n=%d\n" % len(res)); sys.stderr.flush()
except SystemExit:
    sys.stderr.write("SEGFAULT/SystemExit\n"); sys.stderr.flush()
except Exception as e:
    sys.stderr.write("EXC %s\n" % str(e)[:200]); sys.stderr.flush()
