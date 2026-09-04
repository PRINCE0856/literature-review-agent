"""Build small, realistic test PDFs used by the test suite and the mocked E2E run."""
import sys
from pathlib import Path
import pymupdf

PAPERS = [
    ("rainfall_delhi.pdf", "Rainfall Intensity and Mode Choice in Delhi", [
     ("Abstract\n\nThis study examines how rainfall intensity influences mode choice in "
      "Delhi, India. We estimate a mixed logit model using data from a household travel "
      "survey of 2450 respondents collected in 2019. Results show that heavy rainfall "
      "reduces cycling trips by 34 percent and increases auto-rickshaw use by 18 percent."),
     ("1. Introduction\n\nUrban travel behaviour in monsoon climates remains poorly "
      "understood. Little is known about how short-duration high-intensity rainfall "
      "reshapes daily mode choice in Indian cities. This paper addresses that gap."),
     ("2. Methodology\n\nWe use data from the 2019 Delhi household travel survey. The "
      "dependent variable is the chosen travel mode. Independent variables include "
      "rainfall intensity in mm per hour and trip distance. We control for income, age "
      "and gender. Estimation was implemented in Biogeme and Python. The model is "
      "specified as a mixed logit with random coefficients."),
     ("3. Results\n\nResults indicate that rainfall above 10 mm per hour reduces cycling "
      "by 34 percent. Out-of-sample validation on a holdout sample confirms the fit."),
     ("4. Conclusions\n\nPolicymakers should consider sheltered infrastructure at transit "
      "stops. A limitation is that we cannot observe trip chaining. Future research "
      "should examine other Indian cities and longer rainfall records.\n\n"
      "References\n\nSmith, J. (2018). Groundwater recharge in arid basins. Journal of "
      "Hydrology, 12(3), 100-120.")]),
    ("monsoon_mumbai.pdf", "Monsoon Rainfall and Transit Ridership in Mumbai", [
     ("Abstract\n\nThis paper investigates monsoon rainfall and suburban rail ridership in "
      "Mumbai, India. We apply a panel data model to smart card data covering 1200 "
      "stations. Results show ridership increases by 9 percent during moderate rain."),
     ("2. Methods\n\nWe use a fixed effects panel data model. Estimation was implemented "
      "in Stata. We control for temperature and day of week. The unit of analysis is the "
      "station-day."),
     ("3. Discussion\n\nFindings indicate a positive association between moderate rainfall "
      "and rail ridership, contrary to the reduction reported for cycling. Planners "
      "should consider capacity management during monsoon months.\n\n"
      "5. Limitations\n\nA limitation is that smart card data excludes season-ticket "
      "holders. Further research is needed on informal transport modes.")]),
    ("scanned_paper.pdf", "A Scanned Paper With No Text Layer", None),
]

def main(out_dir: str) -> None:
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    for name, title, pages in PAPERS:
        doc = pymupdf.open()
        if pages is None:
            # Image-only pages: a real raster image and no text layer at all, so
            # the file is a realistic size for a scan rather than a stub that the
            # downloader would rightly reject as too small to be a paper.
            import zlib
            width = height = 220
            rows = bytearray()
            for y in range(height):
                rows.append(0)
                for x in range(width):
                    shade = (x * 7 + y * 13) % 256
                    rows += bytes((shade, shade, shade))

            def chunk(tag: bytes, payload: bytes) -> bytes:
                return (
                    len(payload).to_bytes(4, "big") + tag + payload
                    + zlib.crc32(tag + payload).to_bytes(4, "big")
                )

            header = width.to_bytes(4, "big") + height.to_bytes(4, "big")
            png = (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", header + bytes((8, 2, 0, 0, 0)))
                + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
                + chunk(b"IEND", b"")
            )
            for _ in range(2):
                page = doc.new_page()
                page.insert_image(pymupdf.Rect(50, 50, 550, 750), stream=png)
        else:
            for index, body in enumerate(pages):
                page = doc.new_page()
                text = f"{title}\n\n" + body if index == 0 else body
                page.insert_textbox(pymupdf.Rect(50, 50, 550, 780), text,
                                    fontsize=10, fontname="helv")
        doc.set_metadata({"title": title})
        doc.save(out / name); doc.close()
        print(f"wrote {name}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures")
