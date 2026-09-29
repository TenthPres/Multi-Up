#!/usr/bin/env python3
"""
multiup.py - Impose a PDF multi-up onto a larger sheet for printing.

Each source page is embedded as a vector Form XObject.  Nothing is
rasterized, converted, or re-rendered: content streams, images, ICC-based /
CMYK / spot (Separation, DeviceN) color spaces, transparency groups and the
document's OutputIntents are all carried over unchanged.

Examples
    multiup.py cards.pdf 12x18
    multiup.py cards.pdf 12x18 --bleed 0.125 --gutter 0.25 --marks
    multiup.py booklet.pdf 11x17 --mode sequence
"""

import argparse
import math
import sys
from dataclasses import dataclass

import pikepdf
from pikepdf import Array, Dictionary, Name, Pdf, Stream

PT = 72.0  # points per inch
INHERITABLE = ("/Resources", "/MediaBox", "/CropBox", "/Rotate")


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------

def mat_mul(m1, m2):
    """Return m1 followed by m2 (PDF row-vector convention)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def rotation_matrix(deg, w, h):
    """Rotate a w x h box sitting at the origin clockwise by deg (0/90/180/270),
    leaving the result sitting at the origin again."""
    return {0: (1, 0, 0, 1, 0, 0),
            90: (0, -1, 1, 0, 0, w),
            180: (-1, 0, 0, -1, w, h),
            270: (0, 1, -1, 0, h, 0)}[deg % 360]


def parse_size(text):
    """'12x18', '12 x 18', '8.5x11' -> (w_in, h_in)."""
    parts = text.lower().replace(" ", "").split("x")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"size must look like 12x18, got {text!r}")
    try:
        w, h = float(parts[0]), float(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(f"size must look like 12x18, got {text!r}")
    if w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("size must be positive")
    return w, h


def fmt_in(pts):
    return f'{pts / PT:.4g}"'


# --------------------------------------------------------------------------
# Source page inspection
# --------------------------------------------------------------------------

def inherited(page_obj, key):
    node = page_obj
    while node is not None:
        if key in node:
            return node[key]
        node = node.get("/Parent")
    return None


def box(page_obj, key):
    val = inherited(page_obj, key) if key in INHERITABLE else page_obj.get(key)
    if val is None:
        return None
    x0, y0, x1, y1 = (float(v) for v in val)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def intersect(a, b):
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


@dataclass
class SourcePage:
    index: int
    media: tuple      # MediaBox
    trim: tuple       # finished (cut) size, unrotated user space
    bleed: float      # bleed in points available around the trim
    rotate: int       # /Rotate of the source page

    @property
    def trim_w(self):
        return self.trim[2] - self.trim[0]

    @property
    def trim_h(self):
        return self.trim[3] - self.trim[1]


def analyze_page(page_obj, index, bleed_override):
    media = box(page_obj, "/MediaBox")
    crop = box(page_obj, "/CropBox")
    visible = intersect(media, crop) if crop else media
    trim_box = page_obj.get("/TrimBox")
    rotate = int(inherited(page_obj, "/Rotate") or 0) % 360

    if bleed_override is not None:
        b = bleed_override
        if trim_box is not None:
            trim = intersect(box(page_obj, "/TrimBox"), visible)
        else:
            trim = (visible[0] + b, visible[1] + b, visible[2] - b, visible[3] - b)
        return SourcePage(index, media, trim, b, rotate)

    if trim_box is None:
        return None  # caller must decide the bleed

    trim = intersect(box(page_obj, "/TrimBox"), visible)
    bleed_box = box(page_obj, "/BleedBox")
    outer = intersect(bleed_box, visible) if bleed_box else visible
    b = max(0.0, min(trim[0] - outer[0], trim[1] - outer[1],
                     outer[2] - trim[2], outer[3] - trim[3]))
    return SourcePage(index, media, trim, b, rotate)


def ask_bleed():
    prompt = ("The PDF has no TrimBox, so the bleed can't be detected.\n"
              "How much bleed is on each edge of the page, in inches? "
              "[0 = none, e.g. 0.125]: ")
    if not sys.stdin or not sys.stdin.isatty():
        print("warning: no TrimBox and no --bleed given; assuming 0 bleed",
              file=sys.stderr)
        return 0.0
    while True:
        ans = input(prompt).strip()
        if ans == "":
            return 0.0
        try:
            val = float(ans.rstrip('"'))
            if val >= 0:
                return val
        except ValueError:
            pass
        print("Please enter a number of inches, like 0.125")


# --------------------------------------------------------------------------
# Form XObject creation (the colour-preserving part)
# --------------------------------------------------------------------------

def import_obj(out, src, obj):
    """Deep-copy an object (direct or indirect) from src into out."""
    if not isinstance(obj, pikepdf.Object) or not obj.is_indirect:
        if isinstance(obj, (pikepdf.Dictionary, pikepdf.Array, pikepdf.Stream)):
            obj = src.make_indirect(obj)
        else:
            return obj
    return out.copy_foreign(obj)


def page_to_form(out, src, src_page):
    """Wrap a source page as a Form XObject in `out`, byte-for-byte content."""
    obj = src_page.obj
    src_page.contents_coalesce()
    contents = obj.get("/Contents")
    data = contents.read_bytes() if contents is not None else b""

    form = Stream(out, data)
    form.Type = Name.XObject
    form.Subtype = Name.Form
    form.FormType = 1
    form.BBox = Array(box(obj, "/MediaBox"))
    res = inherited(obj, "/Resources")
    form.Resources = import_obj(out, src, res) if res is not None else Dictionary()
    # Transparency group carries the blending colour space - keep it.
    if "/Group" in obj:
        form.Group = import_obj(out, src, obj.Group)
    return out.make_indirect(form)


def copy_document_color_info(src, out):
    """OutputIntents define the intended output profile (PDF/X);
    OCProperties keeps layers referenced by the page content valid."""
    for key in ("/OutputIntents", "/OCProperties"):
        if key in src.Root:
            out.Root[key] = import_obj(out, src, src.Root[key])


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

@dataclass
class Layout:
    cols: int
    rows: int
    rotated: bool     # item turned 90 degrees on the sheet
    cell_w: float     # trim size as placed on the sheet
    cell_h: float
    origin_x: float   # lower-left of the grid (trim of first cell)
    origin_y: float
    gutter: float

    @property
    def count(self):
        return self.cols * self.rows


def fit(sheet_w, sheet_h, item_w, item_h, margin, gutter, bleed, rotate_mode):
    # Outermost items need room for their bleed inside the sheet margin.
    edge = max(margin, bleed)
    avail_w, avail_h = sheet_w - 2 * edge, sheet_h - 2 * edge

    def grid(w, h):
        c = int(math.floor((avail_w + gutter + 1e-6) / (w + gutter)))
        r = int(math.floor((avail_h + gutter + 1e-6) / (h + gutter)))
        return max(c, 0), max(r, 0)

    options = []
    if rotate_mode in ("auto", "no"):
        options.append((False, item_w, item_h))
    if rotate_mode in ("auto", "yes"):
        options.append((True, item_h, item_w))

    best = None
    for rotated, w, h in options:
        c, r = grid(w, h)
        if best is None or c * r > best[0] * best[1]:
            best = (c, r, rotated, w, h)
    c, r, rotated, w, h = best
    if c * r == 0:
        return None
    used_w = c * w + (c - 1) * gutter
    used_h = r * h + (r - 1) * gutter
    return Layout(c, r, rotated, w, h,
                  (sheet_w - used_w) / 2, (sheet_h - used_h) / 2, gutter)


def cell_rect(layout, col, row):
    """Trim rectangle of a cell. Row 0 is the top row."""
    x = layout.origin_x + col * (layout.cell_w + layout.gutter)
    y = layout.origin_y + (layout.rows - 1 - row) * (layout.cell_h + layout.gutter)
    return x, y, x + layout.cell_w, y + layout.cell_h


def placement_ops(sp, form_name, layout, rect, sheet_w, sheet_h):
    """Content-stream operators that place one source page into `rect`."""
    extra = 90 if layout.rotated else 0
    rot = (sp.rotate + extra) % 360
    m = (1, 0, 0, 1, -sp.trim[0], -sp.trim[1])
    m = mat_mul(m, rotation_matrix(rot, sp.trim_w, sp.trim_h))
    m = mat_mul(m, (1, 0, 0, 1, rect[0], rect[1]))

    # Clip to trim + bleed, but never let bleed cross into a neighbour's
    # half of the gutter (with zero gutter, neighbours butt at the trim).
    half = layout.gutter / 2
    b = sp.bleed
    x0, y0, x1, y1 = rect
    gx1, gy1 = sheet_w - layout.origin_x, sheet_h - layout.origin_y
    inner = min(b, half)
    cx0 = x0 - (b if abs(x0 - layout.origin_x) < 1e-3 else inner)
    cy0 = y0 - (b if abs(y0 - layout.origin_y) < 1e-3 else inner)
    cx1 = x1 + (b if abs(x1 - gx1) < 1e-3 else inner)
    cy1 = y1 + (b if abs(y1 - gy1) < 1e-3 else inner)
    cx0, cy0 = max(cx0, 0), max(cy0, 0)
    cx1, cy1 = min(cx1, sheet_w), min(cy1, sheet_h)

    return (f"q {cx0:.4f} {cy0:.4f} {cx1 - cx0:.4f} {cy1 - cy0:.4f} re W n "
            f"{m[0]:.6f} {m[1]:.6f} {m[2]:.6f} {m[3]:.6f} {m[4]:.4f} {m[5]:.4f} cm "
            f"{form_name} Do Q\n")


def registration_colorspace(out):
    """/Separation /All - crop marks print on every plate, as is standard."""
    tint = Dictionary(FunctionType=2, Domain=[0, 1],
                      C0=[0, 0, 0, 0], C1=[1, 1, 1, 1], N=1)
    return out.make_indirect(Array([Name.Separation, Name.All,
                                    Name.DeviceCMYK, out.make_indirect(tint)]))


def crop_mark_ops(layout, bleed, sheet_w, sheet_h):
    offset = bleed + PT / 16          # start mark just outside the bleed
    length = PT / 4
    xs, ys = set(), set()
    for c in range(layout.cols):
        x0, _, x1, _ = cell_rect(layout, c, 0)
        xs.update((round(x0, 3), round(x1, 3)))
    for r in range(layout.rows):
        _, y0, _, y1 = cell_rect(layout, 0, r)
        ys.update((round(y0, 3), round(y1, 3)))
    gx0, gy0 = layout.origin_x, layout.origin_y
    gx1, gy1 = sheet_w - layout.origin_x, sheet_h - layout.origin_y

    ops = ["q /CSReg CS 1 SCN 0.25 w 0 J"]
    room_y = gy0 - offset
    room_x = gx0 - offset
    if room_y > 2:
        ln = min(length, room_y)
        for x in sorted(xs):
            ops.append(f"{x:.4f} {gy0 - offset:.4f} m {x:.4f} {gy0 - offset - ln:.4f} l S")
            ops.append(f"{x:.4f} {gy1 + offset:.4f} m {x:.4f} {gy1 + offset + ln:.4f} l S")
    if room_x > 2:
        ln = min(length, room_x)
        for y in sorted(ys):
            ops.append(f"{gx0 - offset:.4f} {y:.4f} m {gx0 - offset - ln:.4f} {y:.4f} l S")
            ops.append(f"{gx1 + offset:.4f} {y:.4f} m {gx1 + offset + ln:.4f} {y:.4f} l S")
    ops.append("Q\n")
    return "\n".join(ops) if len(ops) > 2 else ""


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def build(args):
    sheet_w, sheet_h = args.sheet[0] * PT, args.sheet[1] * PT
    margin, gutter = args.margin * PT, args.gutter * PT
    bleed_override = None if args.bleed is None else args.bleed * PT

    src = Pdf.open(args.input)
    if len(src.pages) == 0:
        sys.exit("error: input PDF has no pages")

    pages = [analyze_page(p.obj, i, bleed_override) for i, p in enumerate(src.pages)]
    if any(p is None for p in pages):
        bleed_override = ask_bleed() * PT
        pages = [analyze_page(p.obj, i, bleed_override) for i, p in enumerate(src.pages)]

    first = pages[0]
    for p in pages[1:]:
        if abs(p.trim_w - first.trim_w) > 0.5 or abs(p.trim_h - first.trim_h) > 0.5:
            print(f"warning: page {p.index + 1} trim size differs from page 1; "
                  "it will be placed in a page-1-sized cell", file=sys.stderr)
            break
    if any(src.pages[i].obj.get("/Annots") for i in range(len(src.pages))):
        print("warning: annotations/form fields are not carried into the "
              "imposed sheets (flatten them first if they must print)",
              file=sys.stderr)

    # Rotation of the item as it *appears* (after the source /Rotate).
    item_w, item_h = first.trim_w, first.trim_h
    if first.rotate in (90, 270):
        item_w, item_h = item_h, item_w
    bleed = max(p.bleed for p in pages)

    layout = fit(sheet_w, sheet_h, item_w, item_h, margin, gutter, bleed, args.rotate)
    if layout is None:
        sys.exit(f"error: a {fmt_in(item_w)} x {fmt_in(item_h)} page (+{fmt_in(bleed)} "
                 f"bleed) does not fit on a {args.sheet[0]:g}x{args.sheet[1]:g} sheet "
                 f"with {args.margin:g}\" margins")

    out = Pdf.new()
    copy_document_color_info(src, out)
    forms = {i: page_to_form(out, src, src.pages[i]) for i in range(len(pages))}
    reg_cs = registration_colorspace(out) if args.marks else None
    marks = crop_mark_ops(layout, bleed, sheet_w, sheet_h) if args.marks else ""
    sheet_group = next((src.pages[i].obj.Group for i in range(len(pages))
                        if "/Group" in src.pages[i].obj), None)

    # Build the list of sheets: each is a list of (cell_index, page_index).
    sheets = []
    n = layout.count
    if args.mode == "repeat":
        for i in range(len(pages)):
            sheets.append([(c, i) for c in range(n)])
    else:
        order = [i for i in range(len(pages)) for _ in range(args.copies)]
        for s in range(0, len(order), n):
            sheets.append(list(enumerate(order[s:s + n])))

    for sheet_no, cells in enumerate(sheets):
        # Back sides of a duplex job: mirror columns so fronts and backs line
        # up when the sheet is flipped on its long (left/right) edge.
        mirror = args.duplex and sheet_no % 2 == 1
        xobjects = Dictionary()
        ops = []
        for cell, page_idx in cells:
            col, row = cell % layout.cols, cell // layout.cols
            if mirror:
                col = layout.cols - 1 - col
            name = f"/P{page_idx}"
            xobjects[name] = forms[page_idx]
            ops.append(placement_ops(pages[page_idx], name, layout,
                                     cell_rect(layout, col, row), sheet_w, sheet_h))
        ops.append(marks)

        res = Dictionary(XObject=xobjects)
        if reg_cs is not None:
            res.ColorSpace = Dictionary(CSReg=reg_cs)
        page = Dictionary(Type=Name.Page,
                          MediaBox=[0, 0, sheet_w, sheet_h],
                          TrimBox=[0, 0, sheet_w, sheet_h],
                          Resources=res,
                          Contents=out.make_stream("".join(ops).encode("ascii")))
        if sheet_group is not None:
            page.Group = import_obj(out, src, sheet_group)
        out.pages.append(pikepdf.Page(page))

    out.docinfo["/Producer"] = "multiup.py (pikepdf)"
    out.save(args.output, compress_streams=True,
             object_stream_mode=pikepdf.ObjectStreamMode.generate)

    print(f"{len(pages)} source page(s), trim {fmt_in(first.trim_w)} x "
          f"{fmt_in(first.trim_h)}, bleed {fmt_in(bleed)}")
    print(f"{layout.cols} x {layout.rows} = {layout.count} up per sheet"
          f"{' (rotated 90 deg)' if layout.rotated else ''} on "
          f"{args.sheet[0]:g}\" x {args.sheet[1]:g}\"")
    print(f"wrote {len(sheets)} sheet(s) to {args.output}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Impose a PDF multi-up on a larger sheet, preserving colour spaces.")
    ap.add_argument("input", help="source PDF")
    ap.add_argument("sheet", type=parse_size,
                    help="target sheet size in inches, WIDTHxHEIGHT (e.g. 12x18)")
    ap.add_argument("-o", "--output", help="output PDF (default: <input>-multiup.pdf)")
    ap.add_argument("--bleed", type=float, default=None,
                    help="bleed in inches on each edge of the source pages. "
                         "Default: read from TrimBox/BleedBox, or ask if absent.")
    ap.add_argument("--margin", type=float, default=0.25,
                    help="minimum sheet margin in inches (default 0.25)")
    ap.add_argument("--gutter", type=float, default=0.0,
                    help="space between items in inches (default 0 = butt cut)")
    ap.add_argument("--mode", choices=("repeat", "sequence"), default="repeat",
                    help="repeat: fill each sheet with copies of one page "
                         "(step-and-repeat, default); sequence: consecutive pages")
    ap.add_argument("--copies", type=int, default=1,
                    help="sequence mode: copies of each page (default 1)")
    ap.add_argument("--rotate", choices=("auto", "yes", "no"), default="auto",
                    help="turn items 90 deg on the sheet (default auto = whichever fits more)")
    ap.add_argument("--marks", action="store_true",
                    help="add crop marks in registration colour")
    ap.add_argument("--duplex", action="store_true",
                    help="mirror every other sheet for long-edge duplex (repeat mode)")
    args = ap.parse_args(argv)

    if args.output is None:
        base = args.input[:-4] if args.input.lower().endswith(".pdf") else args.input
        args.output = base + "-multiup.pdf"
    if args.copies < 1:
        ap.error("--copies must be at least 1")
    build(args)


if __name__ == "__main__":
    main()
