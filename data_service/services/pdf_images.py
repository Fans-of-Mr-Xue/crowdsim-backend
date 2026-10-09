"""Choose one regional figure by its caption and preserve its actual PDF appearance."""
import re
import unicodedata

import pymupdf

REGIONAL = re.compile(r"区域|地图|分布|流域|路径|范围|空间|行政|积水|雨量|降雨|管网|过载|淹没|内涝|洪水|风险")
CAPTION = re.compile(r"^图\s*\d+\s*[.、:：]?\s*\S+")
DIAGRAM = re.compile(r"架构|流程|框架|功能模块")


def figure_captions(page):
    """Reassemble captions split across blocks by mixed Chinese/Latin fonts."""
    fragments = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            spans = [span for span in line["spans"] if span["text"].strip()]
            if not spans:
                continue
            text = unicodedata.normalize("NFKC", "".join(span["text"] for span in line["spans"])).strip()
            baseline = sum(span["origin"][1] for span in spans) / len(spans)
            fragments.append({"text": text, "box": pymupdf.Rect(line["bbox"]),
                              "baseline": baseline, "size": max(span["size"] for span in spans)})
    rows = []
    for fragment in sorted(fragments, key=lambda part: (part["baseline"], part["box"].x0)):
        # Join only adjacent fragments on the same baseline, never opposite columns.
        row = next((row for row in reversed(rows) if abs(row["baseline"] - fragment["baseline"]) <= 3
                    and (-4 <= fragment["box"].x0 - row["box"].x1 <= 12
                         or -4 <= row["box"].x0 - fragment["box"].x1 <= 12)), None)
        if row is None:
            rows.append(fragment)
        else:
            row["text"] = (fragment["text"] + row["text"] if fragment["box"].x0 < row["box"].x0
                           else row["text"] + fragment["text"])
            row["box"] |= fragment["box"]
    rows.sort(key=lambda row: (row["box"].y0, row["box"].x0))
    for row in rows:
        if not CAPTION.match(row["text"]):
            continue
        caption = row["text"]
        box = pymupdf.Rect(row["box"])
        baseline = row["baseline"]
        # Chinese captions may wrap; stop before English captions or ordinary body text.
        for continuation in rows:
            overlap = min(box.x1, continuation["box"].x1) - max(box.x0, continuation["box"].x0)
            distance = continuation["baseline"] - baseline
            if (0.7 * row["size"] <= distance <= 1.8 * row["size"] and overlap > 30
                    and abs(continuation["size"] - row["size"]) <= 1
                    and re.search(r"[\u4e00-\u9fff]", continuation["text"])
                    and not CAPTION.match(continuation["text"])):
                caption += continuation["text"]
                box |= continuation["box"]
                break
        if REGIONAL.search(caption) and not DIAGRAM.search(caption):
            yield caption, box


def extract_region_image(path):
    with pymupdf.open(path) as document:
        for page_index, page in enumerate(document):
            images = page.get_image_info(xrefs=True)
            for caption, caption_box in figure_captions(page):
                # The figure is directly above its caption in the same column.
                candidates = []
                for image in images:
                    rect = pymupdf.Rect(image["bbox"])
                    overlap = max(0, min(rect.x1, caption_box.x1) - max(rect.x0, caption_box.x0))
                    gap = caption_box.y0 - rect.y1
                    if rect.width >= 70 and rect.height >= 70 and overlap > 30 and -8 <= gap <= 90:
                        candidates.append((max(0, gap), rect, image["xref"]))
                if candidates:
                    _, clip, xref = min(candidates, key=lambda candidate: candidate[0])
                    if xref:
                        # Embedded originals avoid nearby text and PDF overlays in a rendered crop.
                        pixmap = pymupdf.Pixmap(document, xref)
                        if pixmap.n - pixmap.alpha != 3:
                            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
                        scale = min(1, 1600 / max(pixmap.width, pixmap.height))
                        if scale < 1:
                            pixmap = pymupdf.Pixmap(pixmap, int(pixmap.width * scale), int(pixmap.height * scale))
                        return {"data": pixmap.tobytes("png"), "caption": caption[:500], "page": page_index + 1,
                                "width": pixmap.width, "height": pixmap.height}
                else:
                    # Vector figures have no embedded image; render their drawing bounds.
                    drawings = [pymupdf.Rect(item["rect"]) for item in page.get_drawings()]
                    drawings = [rect for rect in drawings if rect.width > 40 and rect.height > 40 and
                                caption_box.y0 - 360 < rect.y0 < caption_box.y0 and rect.y1 <= caption_box.y0 + 5 and
                                rect.x0 >= caption_box.x0 - 100 and rect.x1 <= caption_box.x1 + 100]
                    if not drawings:
                        continue
                    clip = drawings[0]
                    for rect in drawings[1:]:
                        clip |= rect
                clip &= page.rect
                scale = min(3.5, 1600 / max(clip.width, clip.height))
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
                return {"data": pixmap.tobytes("png"), "caption": caption[:500], "page": page_index + 1,
                        "width": pixmap.width, "height": pixmap.height}
    return None
