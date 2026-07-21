"""Generate icon.ico for the packaged Histatu Runner.

Draws a treasure chest (the loot-map theme) on a dark rounded tile at high
resolution, then downsamples into a multi-size .ico. Run with:

    py -3 make_icon.py

Requires Pillow. The output icon.ico is checked in so CI doesn't need to
regenerate it, but this script documents how it was made.
"""
import os
from PIL import Image, ImageDraw

S = 256  # master render size; icon sizes are downsampled from this


def rounded(draw, box, r, fill):
    draw.rounded_rectangle(box, radius=r, fill=fill)


def render(size=S):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = size / 256.0  # scale factor so coords below are in a 256-grid

    def s(v):
        return v * u

    # dark rounded background tile with a subtle top highlight
    rounded(d, [s(8), s(8), s(248), s(248)], s(48), (26, 29, 36, 255))
    rounded(d, [s(8), s(8), s(248), s(120)], s(48), (36, 41, 52, 255))
    rounded(d, [s(8), s(60), s(248), s(248)], s(24), (26, 29, 36, 255))

    gold = (242, 193, 78, 255)
    gold_d = (196, 148, 46, 255)
    wood = (138, 90, 43, 255)
    wood_d = (104, 66, 30, 255)

    # chest body
    bx0, by0, bx1, by1 = s(52), s(120), s(204), s(196)
    rounded(d, [bx0, by0, bx1, by1], s(12), wood)
    # lid (rounded top)
    d.rounded_rectangle([s(48), s(84), s(208), s(140)], radius=s(28), fill=wood_d)
    d.rectangle([s(48), s(120), s(208), s(140)], fill=wood_d)
    # gold bands: outer frame + two verticals + lid rim
    d.rounded_rectangle([s(48), s(84), s(208), s(196)], radius=s(20),
                        outline=gold, width=int(s(9)))
    d.rectangle([s(84), s(84), s(96), s(196)], fill=gold_d)
    d.rectangle([s(160), s(84), s(172), s(196)], fill=gold_d)
    d.rectangle([s(48), s(132), s(208), s(144)], fill=gold)
    # lock plate
    d.rounded_rectangle([s(116), s(128), s(140), s(160)], radius=s(6), fill=gold)
    d.ellipse([s(122), s(140), s(134), s(152)], fill=wood_d)
    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    master = render(S)
    out = os.path.join(here, "icon.ico")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(out, format="ICO", sizes=sizes)
    master.save(os.path.join(here, "icon.png"))  # handy for the web/download page
    print("wrote", out, "and icon.png")


if __name__ == "__main__":
    main()
