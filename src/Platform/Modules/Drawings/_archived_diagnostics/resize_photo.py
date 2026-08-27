import sys
from PIL import Image
path = sys.argv[1]
img = Image.open(path)
w, h = img.size
if max(w, h) > 960:
    s = 960 / max(w, h)
    img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    img.save(path, quality=90, optimize=True)
