"""Mede a compatibilidade do editor contra um acervo de PDFs reais.

Para cada PDF: abre → analisa as primeiras páginas → edita até 2 trechos de texto nativo
(mesmo comprimento e um texto mais longo) → exporta com verificação completa.

Uso:
    python tools/compat_harness.py <pasta-com-pdfs> [--out relatorio.json] [--limit N] [--workers 2]

Cada arquivo roda em um processo separado com tempo limite, então PDFs que travam ou
derrubam o MuPDF não interrompem a medição.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TIMEOUT_S = 60
USE_OCR = False
MAX_PAGES = 3


def _mutate(text: str) -> str:
    """Troca um caractere mantendo o 'tipo' (dígito→dígito, letra→letra) — edição realista."""
    chars = list(text)
    for i in range(len(chars) - 1, -1, -1):
        c = chars[i]
        if c.isdigit():
            chars[i] = "7" if c != "7" else "3"
            return "".join(chars)
        if c.isalpha() and c.isascii():
            chars[i] = ("e" if c != "e" else "a") if c.islower() else ("E" if c != "E" else "A")
            return "".join(chars)
    return text + "1"


def run_one(path: str) -> dict:
    import pymupdf

    from app.engine.analysis import analyze_page
    from app.engine.verify import export_with_verification

    pymupdf.TOOLS.mupdf_display_errors(False)
    res = {"file": Path(path).name, "status": "?", "detail": "", "pages": 0, "edits": 0}
    t0 = time.time()
    try:
        data = Path(path).read_bytes()
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:
            res.update(status="unreadable", detail=str(exc)[:200])
            return res
        if doc.needs_pass:
            res.update(status="password")
            return res
        res["pages"] = doc.page_count
        if doc.page_count == 0:
            res.update(status="no_pages")
            return res
        analyses = {}
        ops = []
        modes = Counter()
        for pno in range(min(MAX_PAGES, doc.page_count)):
            a = analyze_page(doc[pno])
            analyses[pno] = a
            modes[a.mode] += 1
            cands = [e for e in a.elements if e.kind == "native" and len(e.text.strip()) >= 3]
            if not cands or len(ops) >= 2:
                continue
            e = cands[len(cands) // 2]
            ops.append({"type": "edit_text", "page": pno, "elementId": e.id, "text": _mutate(e.text)})
            if len(cands) > 1 and len(ops) < 2:
                e2 = cands[0]
                ops.append({"type": "edit_text", "page": pno, "elementId": e2.id, "text": e2.text + " 2026"})
        res["modes"] = dict(modes)
        if not ops and USE_OCR:
            from app.engine.ocr import ocr_page, ocr_regions

            for pno, a in analyses.items():
                page = doc[pno]
                ocr_regions(page, a)
                if not a.ocr_done and not [e for e in a.elements if e.kind == "ocr"]:
                    ocr_page(page, a, exclude_native=True)
                cands = [e for e in a.elements if e.kind == "ocr" and len(e.text.strip()) >= 3
                         and any(ch.isalnum() for ch in e.text)]
                if cands:
                    e = cands[len(cands) // 2]
                    ops.append({"type": "edit_text", "page": pno, "elementId": e.id, "text": _mutate(e.text)})
                    res["ocr"] = True
                    break
        if not ops:
            res.update(status="no_native_text")
            return res
        res["edits"] = len(ops)
        out, rep = export_with_verification(data, analyses, ops, None)
        res["fonts"] = [o.get("font") for o in rep.ops]
        if out is not None and rep.ok:
            res.update(status="ok")
        else:
            res.update(status="verify_fail", detail=" | ".join(rep.errors)[:400])
    except Exception as exc:
        res.update(status="exception", detail=f"{type(exc).__name__}: {exc}"[:300],
                   trace=traceback.format_exc()[-800:])
    finally:
        res["seconds"] = round(time.time() - t0, 2)
    return res


def _worker(path: str, q: mp.Queue, use_ocr: bool) -> None:
    global USE_OCR
    USE_OCR = use_ocr
    q.put(run_one(path))


def run_isolated(path: str) -> dict:
    q: mp.Queue = mp.Queue()
    p = mp.Process(target=_worker, args=(path, q, USE_OCR))
    p.start()
    p.join(TIMEOUT_S)
    if p.is_alive():
        p.kill()
        p.join()
        return {"file": Path(path).name, "status": "timeout"}
    if p.exitcode != 0 or q.empty():
        return {"file": Path(path).name, "status": "crash", "detail": f"exit {p.exitcode}"}
    return q.get()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--out", default="compat_report.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--only", nargs="*", help="nomes de arquivos específicos")
    ap.add_argument("--ocr", action="store_true", help="usar OCR em páginas sem texto nativo editável")
    ap.add_argument("--from-report", help="testar só os arquivos com status no_native_text deste relatório")
    args = ap.parse_args()
    global USE_OCR, TIMEOUT_S
    USE_OCR = args.ocr
    if args.ocr:
        TIMEOUT_S = 180
    files = sorted(str(p) for p in Path(args.folder).glob("*.pdf"))
    if args.only:
        files = [f for f in files if Path(f).name in set(args.only)]
    if args.from_report:
        prev = json.loads(Path(args.from_report).read_text())
        names = {r["file"] for r in prev["results"] if r["status"] == "no_native_text"}
        files = [f for f in files if Path(f).name in names]
    if args.limit:
        files = files[: args.limit]
    from concurrent.futures import ThreadPoolExecutor

    results = []
    with ThreadPoolExecutor(args.workers) as ex:
        for i, r in enumerate(ex.map(run_isolated, files), 1):
            results.append(r)
            if i % 50 == 0:
                print(f"{i}/{len(files)}", Counter(x["status"] for x in results), flush=True)
    counts = Counter(r["status"] for r in results)
    editable = counts["ok"] + counts["verify_fail"] + counts["exception"] + counts["timeout"] + counts["crash"]
    summary = {
        "total": len(results),
        "counts": dict(counts),
        "editable_attempted": editable,
        "success_rate_on_attempted": round(counts["ok"] / editable, 4) if editable else None,
    }
    Path(args.out).write_text(json.dumps({"summary": summary, "results": results}, indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
