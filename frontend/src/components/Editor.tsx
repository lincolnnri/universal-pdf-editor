import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type DocInfo, type Operation, type PageAnalysis, type TextElement } from "../lib/api";
import { localId, opForElement, removeLocal, setElementOp, updateLocal, useOpsHistory } from "../lib/history";
import type { Draft } from "./InlineEditor";
import { Logo } from "./Logo";
import { PageView, type Active, type Selected, type Tool } from "./PageView";
import { SaveDialog, type SaveState } from "./SaveDialog";
import { Spinner } from "./UploadScreen";

interface Props {
  doc: DocInfo;
  onClose: () => void;
}

type AnalysisState = { data?: PageAnalysis; loading: boolean; error: string | null };
type Preview = { key: string; url: string };

const ZOOMS = [0.5, 0.67, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3];

export function Editor({ doc, onClose }: Props) {
  const history = useOpsHistory();
  const { ops, commit } = history;
  const [zoom, setZoom] = useState(1);
  const [tool, setTool] = useState<Tool>("edit");
  const [active, setActive] = useState<Active>(null);
  const [selected, setSelected] = useState<Selected>(null);
  const [analyses, setAnalyses] = useState<Record<number, AnalysisState>>({});
  const [previews, setPreviews] = useState<Record<number, Preview>>({});
  const [previewLoading, setPreviewLoading] = useState<Record<number, boolean>>({});
  const [currentPage, setCurrentPage] = useState(0);
  const [save, setSave] = useState<SaveState>({ phase: "closed" });
  const [savedKey, setSavedKey] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const visibility = useRef<Record<number, number>>({});
  const fileInput = useRef<HTMLInputElement>(null);
  const requested = useRef<Set<number>>(new Set());
  const pendingImageAt = useRef<{ page: number; x: number; y: number } | null>(null);

  const renderScale = useMemo(() => {
    const dpr = window.devicePixelRatio || 1;
    return Math.min(3, Math.max(0.5, Math.round(zoom * dpr * 2) / 2));
  }, [zoom]);

  // zoom inicial: ajusta à largura disponível
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const maxW = Math.max(...doc.pages.map((p) => p.width));
    const fit = (el.clientWidth - 64) / maxW;
    const z = ZOOMS.filter((v) => v <= Math.min(fit, 1.5)).pop() ?? 0.5;
    setZoom(z);
  }, [doc.pages]);

  // ------------------------------------------------------------------ análise
  const loadAnalysis = useCallback(
    async (page: number, ocr: "auto" | "force" = "auto") => {
      setAnalyses((a) => ({ ...a, [page]: { ...a[page], loading: true, error: null } }));
      try {
        const data = await api.analysis(doc.id, page, ocr);
        setAnalyses((a) => ({ ...a, [page]: { data, loading: false, error: null } }));
      } catch (e) {
        setAnalyses((a) => ({
          ...a,
          [page]: { ...a[page], loading: false, error: e instanceof Error ? e.message : "Falha ao analisar a página." },
        }));
      }
    },
    [doc.id],
  );

  const onVisible = useCallback(
    (page: number, ratio: number) => {
      visibility.current[page] = ratio;
      if (ratio > 0 && !requested.current.has(page)) {
        requested.current.add(page);
        void loadAnalysis(page);
      }
      const best = Object.entries(visibility.current).sort((x, y) => y[1] - x[1])[0];
      if (best && best[1] > 0) setCurrentPage(Number(best[0]));
    },
    [loadAnalysis],
  );

  // ------------------------------------------------------------------ prévia real (servidor)
  const opsByPage = useMemo(() => {
    const m: Record<number, Operation[]> = {};
    ops.forEach((o) => (m[o.page] ??= []).push(o));
    return m;
  }, [ops]);

  const previewsRef = useRef(previews);
  previewsRef.current = previews;
  const controllers = useRef<Record<number, AbortController>>({});

  useEffect(() => {
    const timers: number[] = [];
    for (let page = 0; page < doc.pageCount; page++) {
      const pageOps = opsByPage[page] ?? [];
      const key = JSON.stringify([renderScale, pageOps.map(({ ...o }) => ({ ...o, previewUrl: undefined }))]);
      const cur = previewsRef.current[page];
      if (!pageOps.length) {
        if (cur) {
          URL.revokeObjectURL(cur.url);
          setPreviews((p) => {
            const n = { ...p };
            delete n[page];
            return n;
          });
        }
        controllers.current[page]?.abort();
        setPreviewLoading((l) => (l[page] ? { ...l, [page]: false } : l));
        continue;
      }
      if (cur?.key === key) continue;
      setPreviewLoading((l) => ({ ...l, [page]: true }));
      timers.push(
        window.setTimeout(async () => {
          controllers.current[page]?.abort();
          const ctrl = new AbortController();
          controllers.current[page] = ctrl;
          try {
            const blob = await api.preview(doc.id, page, pageOps, renderScale, ctrl.signal);
            const url = URL.createObjectURL(blob);
            setPreviews((p) => {
              if (p[page]) URL.revokeObjectURL(p[page].url);
              return { ...p, [page]: { key, url } };
            });
            setPreviewError(null);
          } catch (e) {
            if ((e as Error).name === "AbortError") return;
            setPreviewError(e instanceof Error ? e.message : "Falha ao aplicar a alteração.");
          } finally {
            if (controllers.current[page] === ctrl) setPreviewLoading((l) => ({ ...l, [page]: false }));
          }
        }, 150),
      );
    }
    return () => timers.forEach((t) => window.clearTimeout(t));
  }, [opsByPage, renderScale, doc.id, doc.pageCount]);

  // ------------------------------------------------------------------ ações
  const commitElement = useCallback(
    (el: TextElement, d: Draft) => {
      setActive(null);
      const text = d.text;
      let op: Operation | null;
      if (!text.trim()) op = { type: "delete_element", page: el.page, elementId: el.id };
      else {
        const styleSame = Math.abs(d.size - el.size) < 0.05 && d.color.toLowerCase() === el.color.toLowerCase() &&
          d.bold === el.bold && d.italic === el.italic && d.align === el.align;
        if (text === el.text && styleSame) op = null;
        else {
          op = { type: "edit_text", page: el.page, elementId: el.id, text };
          if (Math.abs(d.size - el.size) >= 0.05) op.size = d.size;
          if (d.color.toLowerCase() !== el.color.toLowerCase()) op.color = d.color;
          if (d.bold !== el.bold) op.bold = d.bold;
          if (d.italic !== el.italic) op.italic = d.italic;
          if (d.align !== el.align) op.align = d.align;
        }
      }
      const existing = opForElement(ops, el.id);
      if (!op && !existing) return;
      if (op && existing && JSON.stringify(op) === JSON.stringify(existing)) return;
      commit(setElementOp(ops, el.id, op));
    },
    [ops, commit],
  );

  const deleteElement = useCallback(
    (page: number, elementId: string) => {
      setActive(null);
      setSelected(null);
      commit(setElementOp(ops, elementId, { type: "delete_element", page, elementId }));
    },
    [ops, commit],
  );

  const commitAdded = useCallback(
    (id: string, d: Draft) => {
      setActive(null);
      const cur = ops.find((o) => "localId" in o && o.localId === id);
      if (!cur || cur.type !== "add_text") return;
      if (!d.text.trim()) {
        commit(removeLocal(ops, id));
        return;
      }
      const next = {
        text: d.text, size: d.size, color: d.color, bold: d.bold, italic: d.italic,
        fontFamily: d.mono ? "mono" : d.serif ? "serif" : "sans",
      } as Partial<Operation>;
      const changed = (Object.keys(next) as (keyof typeof next)[]).some(
        (k) => (cur as Record<string, unknown>)[k] !== (next as Record<string, unknown>)[k],
      );
      if (changed) commit(updateLocal(ops, id, next));
    },
    [ops, commit],
  );

  const setField = useCallback(
    (page: number, field: { id: string; value: string }, value: string | boolean) => {
      const original = field.value;
      const same = typeof value === "boolean" ? value === (original !== "Off" && original !== "") : value === original;
      commit(setElementOp(ops, field.id, same ? null : { type: "set_field", page, elementId: field.id, value }));
    },
    [ops, commit],
  );

  const removeAdded = useCallback(
    (id: string) => {
      setActive(null);
      setSelected(null);
      commit(removeLocal(ops, id));
    },
    [ops, commit],
  );

  const moveAdded = useCallback(
    (id: string, dx: number, dy: number, dw?: number, dh?: number) => {
      const cur = ops.find((o) => "localId" in o && o.localId === id);
      if (!cur) return;
      if (cur.type === "add_text") commit(updateLocal(ops, id, { x: cur.x + dx, y: cur.y + dy } as Partial<Operation>));
      else if (cur.type === "add_image") {
        const [x0, y0, x1, y1] = cur.rect;
        const rect: [number, number, number, number] = dw !== undefined
          ? [x0, y0, Math.max(x0 + 8, x1 + dw), Math.max(y0 + 8, y1 + (dh ?? 0))]
          : [x0 + dx, y0 + dy, x1 + dx, y1 + dy];
        commit(updateLocal(ops, id, { rect } as Partial<Operation>));
      }
    },
    [ops, commit],
  );

  const placeText = useCallback(
    (page: number, x: number, y: number) => {
      const id = localId("txt");
      const op: Operation = { type: "add_text", page, x, y: y - 8, text: "Novo texto", size: 12, color: "#000000", localId: id };
      commit([...ops, op]);
      setTool("edit");
      setActive({ kind: "added", page, localId: id });
    },
    [ops, commit],
  );

  const placeImage = useCallback((page: number, x: number, y: number) => {
    pendingImageAt.current = { page, x, y };
    fileInput.current?.click();
  }, []);

  const onImageFile = async (file: File) => {
    const at = pendingImageAt.current ?? { page: currentPage, x: 72, y: 72 };
    pendingImageAt.current = null;
    try {
      const res = await api.uploadImage(doc.id, file);
      const page = doc.pages[at.page];
      const maxW = Math.min(200, page.width * 0.5);
      const w = Math.min(maxW, res.width * 0.75);
      const h = (w * res.height) / res.width;
      const id = localId("img");
      const op: Operation = {
        type: "add_image", page: at.page, imageId: res.imageId, localId: id,
        rect: [at.x, at.y, Math.min(page.width, at.x + w), Math.min(page.height, at.y + h)],
        previewUrl: URL.createObjectURL(file),
      };
      commit([...ops, op]);
      setSelected({ kind: "addedImage", page: at.page, id });
      setTool("edit");
    } catch (e) {
      setToast(e instanceof Error ? e.message : "Não foi possível adicionar a imagem.");
    }
  };

  // ------------------------------------------------------------------ salvar
  const [chosenPages, setChosenPages] = useState<number[] | null>(null);
  const runExport = useCallback(
    async (ack: boolean, pages: number[] | null = chosenPages) => {
      setActive(null);
      setSave({ phase: "working" });
      try {
        const report = await api.exportPdf(doc.id, ops, ack, pages);
        if (!report.ok || !report.downloadUrl) {
          setSave({ phase: "error", message: "A verificação do PDF gerado falhou.", report });
          return;
        }
        setSavedKey(JSON.stringify(ops));
        setSave({ phase: "done", report });
        const a = document.createElement("a");
        a.href = report.downloadUrl;
        a.download = report.filename ?? "documento-editado.pdf";
        document.body.appendChild(a);
        a.click();
        a.remove();
      } catch (e) {
        if (e instanceof ApiError && e.code === "signature_ack_required") {
          setSave({ phase: "confirm-signature" });
          return;
        }
        if (e instanceof ApiError) setSave({ phase: "error", message: e.message, report: e.report });
        else setSave({ phase: "error", message: "Não foi possível comunicar com o servidor. Verifique a conexão e tente novamente." });
      }
    },
    [doc.id, ops, chosenPages],
  );

  const startSave = () => {
    if (!ops.length && doc.pages.length === 1) {
      setToast("Nenhuma alteração para salvar. Clique em um texto do documento para editá-lo.");
      return;
    }
    setActive(null);
    if (doc.pages.length > 1) {
      setSave({ phase: "choose-pages" });
      return;
    }
    setChosenPages(null);
    if (doc.signed) setSave({ phase: "confirm-signature" });
    else void runExport(false, null);
  };

  const onChoosePages = (pages: number[] | null) => {
    setChosenPages(pages);
    if (doc.signed && ops.length) setSave({ phase: "confirm-signature" });
    else void runExport(false, pages);
  };

  const dirty = ops.length > 0 && savedKey !== JSON.stringify(ops);

  // ------------------------------------------------------------------ teclado
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (target.isContentEditable || ["INPUT", "TEXTAREA"].includes(target.tagName)) return;
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key.toLowerCase() === "z" && !e.shiftKey) {
        e.preventDefault();
        history.undo();
      } else if (mod && (e.key.toLowerCase() === "y" || (e.key.toLowerCase() === "z" && e.shiftKey))) {
        e.preventDefault();
        history.redo();
      } else if (mod && e.key.toLowerCase() === "s") {
        e.preventDefault();
        startSave();
      } else if ((e.key === "Delete" || e.key === "Backspace") && selected) {
        e.preventDefault();
        if (selected.kind === "image") deleteElement(selected.page, selected.id);
        else removeAdded(selected.id);
      } else if (e.key === "Escape") {
        setSelected(null);
        setTool("edit");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 5000);
    return () => window.clearTimeout(t);
  }, [toast]);

  const zoomTo = (dir: 1 | -1) => {
    const i = ZOOMS.findIndex((z) => z >= zoom - 1e-6);
    const next = ZOOMS[Math.min(ZOOMS.length - 1, Math.max(0, (i < 0 ? ZOOMS.length - 1 : i) + dir))];
    setZoom(next);
  };

  const scrollToPage = (n: number) => {
    scroller.current?.querySelector(`[data-page="${n}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const cur = analyses[currentPage];

  return (
    <div className="flex h-full flex-col">
      {/* barra superior */}
      <header className="flex h-14 shrink-0 items-center gap-3 whitespace-nowrap border-b border-ink-200 bg-night-900/95 px-4 backdrop-blur">
        <Logo compact />
        <span className="ml-2 min-w-[5rem] max-w-[16rem] shrink truncate text-sm text-ink-600" title={doc.filename}>
          {doc.filename}
        </span>
        <div className="mx-2 h-6 w-px bg-ink-200" />
        <IconBtn title="Desfazer (Ctrl+Z)" disabled={!history.canUndo} onClick={history.undo} testid="undo">
          <path d="M9 14L4 9l5-5M4 9h11a5 5 0 0 1 0 10h-3" />
        </IconBtn>
        <IconBtn title="Refazer (Ctrl+Y)" disabled={!history.canRedo} onClick={history.redo} testid="redo">
          <path d="M15 14l5-5-5-5M20 9H9a5 5 0 0 0 0 10h3" />
        </IconBtn>
        <div className="mx-1 h-6 w-px bg-ink-200" />
        <div className="flex rounded-lg bg-ink-100 p-0.5 text-sm">
          <ToolTab active={tool === "edit"} onClick={() => setTool("edit")} testid="tool-edit">Editar texto</ToolTab>
          <ToolTab active={tool === "text"} onClick={() => setTool("text")} testid="tool-text">+ Texto</ToolTab>
          <ToolTab active={tool === "image"} onClick={() => setTool("image")} testid="tool-image">+ Imagem</ToolTab>
        </div>
        <button
          className="rounded-lg px-2.5 py-1.5 text-sm text-ink-600 hover:bg-ink-100 disabled:opacity-40"
          disabled={!cur || cur.loading || cur.data?.ocrDone}
          onClick={() => void loadAnalysis(currentPage, "force")}
          title="Reconhece textos que fazem parte de imagens nesta página"
        >
          {cur?.data?.ocrDone ? "OCR concluído" : "OCR na página"}
        </button>
        <div className="flex-1" />
        <div className="flex items-center gap-1 text-sm">
          <IconBtn title="Diminuir zoom" onClick={() => zoomTo(-1)}>
            <path d="M5 12h14" />
          </IconBtn>
          <span className="w-12 text-center tabular-nums text-ink-600">{Math.round(zoom * 100)}%</span>
          <IconBtn title="Aumentar zoom" onClick={() => zoomTo(1)}>
            <path d="M12 5v14M5 12h14" />
          </IconBtn>
        </div>
        <div className="mx-1 h-6 w-px bg-ink-200" />
        <span className="hidden text-xs text-ink-400 lg:block" data-testid="save-status">
          {ops.length === 0 ? "Sem alterações" : dirty ? `Não salvo · ${ops.length} alteração(ões)` : "Salvo e verificado"}
        </span>
        <button
          onClick={startSave}
          disabled={save.phase === "working"}
          className="flex items-center gap-2 rounded-lg bg-brand-500 px-4 py-2 text-sm font-semibold text-night-950 hover:bg-brand-600 disabled:opacity-60"
          data-testid="save"
        >
          {save.phase === "working" && <Spinner className="h-4 w-4" />}
          Salvar e baixar PDF
        </button>
        <IconBtn
          title="Fechar documento"
          onClick={() => {
            if (!dirty || confirm("Há alterações não salvas. Fechar mesmo assim?")) onClose();
          }}
        >
          <path d="M6 6l12 12M18 6L6 18" />
        </IconBtn>
      </header>

      {doc.warnings.length > 0 && (
        <div className="border-b border-amber-500/30 bg-amber-500/10 px-4 py-2 text-sm text-amber-200">{doc.warnings.join(" ")}</div>
      )}
      {(previewError || toast) && (
        <div className="border-b border-red-500/30 bg-red-500/10 px-4 py-2 text-sm text-red-300" role="alert">
          {previewError ?? toast}
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        {/* miniaturas */}
        <aside className="thin-scroll hidden w-44 shrink-0 overflow-y-auto border-r border-ink-200 bg-night-900 p-3 md:block">
          {doc.pages.map((p) => {
            const thumb = previews[p.number]?.url ?? api.pageImageUrl(doc.id, p.number, 0.25);
            const edited = (opsByPage[p.number]?.length ?? 0) > 0;
            return (
              <button
                key={p.number}
                onClick={() => scrollToPage(p.number)}
                className={`mb-3 block w-full rounded-lg p-1.5 text-left ${currentPage === p.number ? "bg-brand-50 ring-2 ring-brand-500" : "hover:bg-ink-100"}`}
              >
                <img src={thumb} loading="lazy" alt="" className="w-full border border-ink-200 bg-white" style={{ aspectRatio: `${p.width} / ${p.height}` }} />
                <span className="mt-1 flex items-center justify-between text-xs text-ink-600">
                  {p.number + 1}
                  {edited && <span className="h-2 w-2 rounded-full bg-emerald-500/100" title="Página editada" />}
                </span>
              </button>
            );
          })}
        </aside>

        {/* documento */}
        <main ref={scroller} className="thin-scroll relative flex-1 overflow-auto bg-transparent">
          <div className="space-y-8 px-8 py-8">
            {doc.pages.map((p) => {
              const a = analyses[p.number];
              return (
                <PageView
                  key={p.number}
                  page={p}
                  zoom={zoom}
                  imageUrl={previews[p.number]?.url ?? api.pageImageUrl(doc.id, p.number, renderScale)}
                  previewLoading={!!previewLoading[p.number]}
                  analysis={a?.data}
                  analysisLoading={!!a?.loading}
                  analysisError={a?.error ?? null}
                  ops={opsByPage[p.number] ?? []}
                  tool={tool}
                  active={active}
                  selected={selected}
                  onVisible={onVisible}
                  onActivate={(x) => {
                    setSelected(null);
                    setActive(x);
                  }}
                  onSelect={setSelected}
                  onCommitElement={commitElement}
                  onDeleteElement={deleteElement}
                  onCommitAdded={commitAdded}
                  onRemoveAdded={removeAdded}
                  onMoveAdded={moveAdded}
                  onPlaceText={placeText}
                  onPlaceImage={placeImage}
                  onSetField={setField}
                />
              );
            })}
          </div>
        </main>
      </div>

      {tool !== "edit" && (
        <div className="pointer-events-none fixed bottom-6 left-1/2 z-40 -translate-x-1/2 rounded-full bg-night-800 px-4 py-2 text-sm text-ink-900 shadow-glow ring-1 ring-brand-500/40">
          {tool === "text" ? "Clique na página onde deseja adicionar o texto" : "Clique na página onde deseja inserir a imagem"} · Esc para cancelar
        </div>
      )}

      <input
        ref={fileInput}
        type="file"
        accept="image/png,image/jpeg,image/webp,image/gif"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void onImageFile(f);
          e.target.value = "";
        }}
      />

      <SaveDialog
        state={save}
        onClose={() => setSave({ phase: "closed" })}
        onRetry={() => void runExport(doc.signed)}
        onConfirmSignature={() => void runExport(true)}
        pageCount={doc.pages.length}
        editedPages={Object.keys(opsByPage).map(Number).filter((p) => (opsByPage[p]?.length ?? 0) > 0)}
        hasEdits={ops.length > 0}
        thumbUrl={(p) => previews[p]?.url ?? api.pageImageUrl(doc.id, p, 0.25)}
        onChoosePages={onChoosePages}
      />
    </div>
  );
}

function IconBtn({ children, title, onClick, disabled, testid }: { children: React.ReactNode; title: string; onClick: () => void; disabled?: boolean; testid?: string }) {
  return (
    <button
      title={title}
      aria-label={title}
      onClick={onClick}
      disabled={disabled}
      data-testid={testid}
      className="grid h-9 w-9 place-items-center rounded-lg text-ink-600 hover:bg-ink-100 disabled:opacity-30 disabled:hover:bg-transparent"
    >
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        {children}
      </svg>
    </button>
  );
}

function ToolTab({ active, onClick, children, testid }: { active: boolean; onClick: () => void; children: React.ReactNode; testid?: string }) {
  return (
    <button
      onClick={onClick}
      data-testid={testid}
      className={`rounded-md px-3 py-1.5 font-medium ${active ? "bg-night-700 text-brand-500 shadow-sm" : "text-ink-600 hover:text-ink-900"}`}
    >
      {children}
    </button>
  );
}
