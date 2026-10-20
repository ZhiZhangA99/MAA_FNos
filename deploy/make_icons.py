import os
from PIL import Image

SRC = "/work/maa-logo-512.png"
OUT = "/work/out"
os.makedirs(OUT, exist_ok=True)

img = Image.open(SRC).convert("RGBA")
print("source:", img.size, img.mode)

def save(size, *names):
    im = img.resize((size, size), Image.LANCZOS)
    for n in names:
        p = os.path.join(OUT, n)
        im.save(p, "PNG", optimize=True)
        print(f"{n}: {im.size} {os.path.getsize(p)} bytes")

save(256, "ICON_256.PNG", "icon_256.png")
save(64, "ICON.PNG", "icon_64.png")
print("done")
