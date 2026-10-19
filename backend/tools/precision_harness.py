"""Mede a PRECISÃO visual da edição de texto nativo.

Para cada PDF: escolhe até N trechos nativos, troca UM caractere (dígito→dígito, letra→letra),
exporta com verificação e compara a renderização antes/depois DENTRO da caixa do trecho,
excluindo o caractere trocado. Num editor perfeito os outros glifos não se movem: deriva = 0.

Uso: python tools/precision_harness.py <pasta> [--limit N] [--out precision.json]
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TIMEOUT_S = 90
PER_DOC = 4
DPI = 144


def _mutate(text: str) -> tuple[str, int] | None:
    chars = list(text)
    for i in range(len(chars) - 1, -1, -1):
        c = chars[i]
        if c.isdigit():
            chars[i] = "7" if c != "7" else "3"
            return "".join(chars), i
        if c.isalpha() and c.isascii():
            chars[i] = ("e" if c != "e" else "a") if c.islower() else ("E" if c != "E" else "A")
            return "".join(chars), i
    return None


def _render(page, clip):
    import numpy as np
    import pymupdf

    pix = page.get_pixmap(dpi=DPI, clip=clip, colorspace=pymupdf.csGRAY, annots=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).astype(np.int16)


def run_one(path: str) -> dict:
    import numpy as np
    import pymupdf

    from app.engine.analysis import analyze_page, to_display_rect
    from app.engine.verify import export_with_verification

    pymupdf.TOOLS.mupdf_display_errors(False)
    res = {"file": Path(path).name, "edits": []}
    data = Path(path).read_bytes()
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        res["status"] = "unreadable"
        return res
    if doc.needs_pass or doc.page_count == 0:
        res["status"] = "skip"
        return res
    import zlib
    rnd = random.Random(zlib.crc32(Path(path).name.encode()))
    for pno in range(min(2, doc.page_count)):
        a = analyze_page(doc[pno])
        cands = [e for e in a.elements if e.kind == "native" and not e.is_paragraph
                 and len(e.text.strip()) >= 4 and _mutate(e.text) and len(e.char_rects) == len(e.text)]
        rnd.shuffle(cands)
        for e in cands[: PER_DOC - len(res["edits"])]:
            new, idx = _mutate(e.text)
            ops = [{"type": "edit_text", "page": pno, "elementId": e.id, "text": new}]
            t0 = time.time()
            try:
                out, rep = export_with_verification(data, {pno: a}, ops, None)
            except Exception as exc:  # noqa: BLE001
                res["edits"].append({"status": "exception", "detail": str(exc)[:200]})
                continue
            item = {"text": e.text[:40], "new": new[:40], "secs": round(time.time() - t0, 2),
                    "font": rep.ops[0].get("font") if rep.ops else None}
            if out is None or not rep.ok:
                item.update(status="verify_fail", detail=" | ".join(rep.errors)[:200])
                res["edits"].append(item)
                continue
            od = pymupdf.open(stream=out, filetype="pdf")
            p0, p1 = doc[pno], od[pno]
            box = to_display_rect(p0, e.rect_unrot()) + (-1, -1, 1, 1)
            A, B = _render(p0, box), _render(p1, box)
            if A.shape != B.shape:
                item.update(status="shape")
                res["edits"].append(item)
                continue
            # máscara: exclui o caractere trocado
            sc = DPI / 72.0
            cr = to_display_rect(p0, pymupdf.Rect(e.char_rects[idx])) + (-0.6, -0.6, 0.6, 0.6)
            mask = np.ones(A.shape, bool)
            x0, y0 = int((cr.x0 - box.x0) * sc), int((cr.y0 - box.y0) * sc)
            x1, y1 = int((cr.x1 - box.x0) * sc) + 1, int((cr.y1 - box.y0) * sc) + 1
            mask[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = False
            ink = ((A < 160) & mask).sum()
            diff = ((np.abs(A - B) > 60) & mask).sum()
            item.update(status="ok", drift=round(float(diff) / max(1, ink), 4), ink=int(ink))
            res["edits"].append(item)
        if len(res["edits"]) >= PER_DOC:
            break
    res["status"] = "done"
    return res


def _worker(path, q):
    q.put(run_one(path))


def run_isolated(path: str) -> dict:
    q = mp.Queue()
    p = mp.Process(target=_worker, args=(path, q))
    p.start()
    p.join(TIMEOUT_S)
    if p.is_alive():
        p.kill()
        p.join()
        return {"file": Path(path).name, "status": "timeout", "edits": []}
    if q.empty():
        return {"file": Path(path).name, "status": "crash", "edits": []}
    return q.get()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="precision.json")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    files = sorted(str(p) for p in Path(args.folder).glob("*.pdf"))
    if args.only:
        files = [f for f in files if Path(f).name in set(args.only)]
    if args.limit:
        files = files[: args.limit]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(run_isolated, files))
    edits = [e for r in results for e in r["edits"]]
    ok = [e for e in edits if e.get("status") == "ok"]
    drifts = sorted(e["drift"] for e in ok)
    summ = {
        "files": len(files), "edits": len(edits),
        "ok": len(ok), "verify_fail": sum(e.get("status") == "verify_fail" for e in edits),
        "exception": sum(e.get("status") == "exception" for e in edits),
        "perfect(<1%)": sum(d < 0.01 for d in drifts),
        "good(<5%)": sum(d < 0.05 for d in drifts),
        "bad(>=20%)": sum(d >= 0.2 for d in drifts),
        "median_drift": drifts[len(drifts) // 2] if drifts else None,
    }
    Path(args.out).write_text(json.dumps({"summary": summ, "results": results}, indent=1, ensure_ascii=False, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
    print(json.dumps(summ, indent=1, default=lambda o: o.item()))


if __name__ == "__main__":
    main()
