# multiup

Imposes a PDF multi-up onto a larger press sheet for printing.

Each source page is embedded as a vector Form XObject, so nothing is rasterized or
color-converted. CMYK, ICC-based, spot (Separation/DeviceN) colors, transparency
groups, and the document's OutputIntents all pass through unchanged. Crop marks
use the `/All` registration color, so they print on every plate.

## Usage

```
multiup.py INPUT.pdf WIDTHxHEIGHT [options]

  -o, --output FILE     default: INPUT-multiup.pdf
  --bleed IN            bleed per edge in inches. By default it is read from the
                        TrimBox/BleedBox. If the PDF has no TrimBox, you are asked.
  --margin IN           minimum sheet margin (default 0.25)
  --gutter IN           space between items (default 0 = butt cut)
  --mode repeat|sequence
                        repeat: each sheet is filled with one page (default)
                        sequence: consecutive pages flow across the sheets
  --copies N            sequence mode only: copies of each page
  --rotate auto|yes|no  turn items 90 deg; auto picks whichever fits more
  --marks               add crop marks
  --duplex              mirror every other sheet for long-edge duplex (fronts/backs)
```

Examples:

```
python multiup.py cards.pdf 12x18 --marks
python multiup.py cards.pdf 13x19 --bleed 0.125 --gutter 0.25 --marks --duplex
python multiup.py flyers.pdf 11x17 --mode sequence
```

With a gutter, each item keeps up to half the gutter as bleed. With no gutter,
neighbors meet at the trim line. The outer edges always keep the full bleed.

## Building the Windows .exe

Run `build_exe.bat`. It produces `dist\multiup.exe`, a single file of about 32 MB
that runs without Python installed.

### Releases

Pushing a tag that starts with `v` builds the exe on GitHub Actions and attaches
it to a new GitHub Release. The version shown by `multiup --version` is taken
from the tag.

```
git tag v1.0.0
git push origin v1.0.0
```

## License

MIT. See [LICENSE](LICENSE).

## Limitations

- Annotations and form fields are not carried over. Flatten them first.
- Every page is placed in a cell sized to page 1's trim. If page sizes differ,
  you get a warning.
