import { useEffect, useRef, useState } from "react";
import type { FormField, Operation, PageAnalysis, PageInfo, TextElement } from "../lib/api";
import { measureText } from "../lib/text";
import { InlineEditor, type Draft } from "./InlineEditor";
import { Spinner } from "./UploadScreen";

export type Tool = "edit" | "text" | "image";

export type Active =
  | { kind: "element"; page: number; elementId: string }
  | { kind: "added"; page: number; localId: string }
  | null;

export type Selected = { kind: "image" | "addedImage" | "addedText"; page: number; id: string } | null;

interface Props {
  page: PageInfo;
  zoom: number;
  imageUrl: string;
  previewLoading: boolean;
  analysis: PageAnalysis | undefined;
  analysisError: string | null;
  analysisLoading: boolean;
  ops: Operation[];
  tool: Tool;
  active: Active;
  selected: Selected;
  onVisible: (page: number, ratio: number) => void;
  onActivate: (a: Active) => void;
  onSelect: (s: Selected) => void;
  onCommitElement: (el: TextElement, d: Draft) => void;
  onDeleteElement: (pageNo: number, elementId: string) => void;
  onCommitAdded: (localId: string, d: Draft) => void;
  onRemoveAdded: (localId: string) => void;
  onMoveAdded: (localId: string, dx: number, dy: number, dw?: number, dh?: number) => void;
  onPlaceText: (page: number, x: number, y: number) => void;
  onPlaceImage: (page: number, x: number, y: number) => void;
  onSetField: (pageNo: number, field: FormField, value: string | boolean) => void;
}

export function PageView(p: Props) {
  const { page, zoom, analysis, ops } = p;
  const ref = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<{ id: string; dx: number; dy: number; dw: number; dh: number } | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => p.onVisible(page.number, e.isIntersecting ? e.intersectionRatio : 0)),
      { threshold: [0, 0.1, 0.25, 0.5, 0.75, 1], rootMargin: "300px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page.number]);

  const W = page.width * zoom;
  const H = page.height * zoom;
  const elementOps = new Map<string, Operation>();
  ops.forEach((o) => {
    if ("elementId" in o) elementOps.set(o.elementId, o);
  });

  const toPt = (e: React.MouseEvent) => {
    const r = ref.current!.getBoundingClientRect();
    return { x: (e.clientX - r.left) / zoom, y: (e.clientY - r.top) / zoom };
  };

  const onBackgroundClick = (e: React.MouseEvent) => {
    if (e.target !== e.currentTarget) return;
    const { x, y } = toPt(e);
    if (p.tool === "text") p.onPlaceText(page.number, x, y);
    else if (p.tool === "image") p.onPlaceImage(page.number, x, y);
    else p.onSelect(null);
  };

  const startDrag = (e: React.MouseEvent, id: string, mode: "move" | "resize", onClick?: () => void) => {
    e.preventDefault();
    e.stopPropagation();
    const sx = e.clientX;
    const sy = e.clientY;
    let moved = false;
    const move = (ev: MouseEvent) => {
      const dx = (ev.clientX - sx) / zoom;
      const dy = (ev.clientY - sy) / zoom;
      if (Math.abs(dx) + Math.abs(dy) > 1.5) moved = true;
      setDrag(mode === "move" ? { id, dx, dy, dw: 0, dh: 0 } : { id, dx: 0, dy: 0, dw: dx, dh: dy });
    };
    const up = (ev: MouseEvent) => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      setDrag(null);
      const dx = (ev.clientX - sx) / zoom;
      const dy = (ev.clientY - sy) / zoom;
      if (moved) {
        if (mode === "move") p.onMoveAdded(id, dx, dy);
        else p.onMoveAdded(id, 0, 0, dx, dy);
      } else onClick?.();
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  const editingElement =
    p.active?.kind === "element" && p.active.page === page.number
      ? analysis?.elements.find((e) => e.id === (p.active as { elementId: string }).elementId)
      : undefined;
  const editingAdded =
    p.active?.kind === "added" && p.active.page === page.number
      ? ops.find((o) => o.type === "add_text" && o.localId === (p.active as { localId: string }).localId)
      : undefined;

  const cursor = p.tool === "text" ? "cursor-text" : p.tool === "image" ? "cursor-copy" : "cursor-default";

  return (
    <div className="relative mx-auto" style={{ width: W }} data-page={page.number}>
      <div className="mb-1.5 flex items-center justify-between text-xs text-ink-400">
        <span>Página {page.number + 1}</span>
        <span className="flex gap-2">
          {analysis?.mode === "scanned" && <Badge tone="amber">Digitalizada · OCR</Badge>}
          {analysis?.mode === "ocr_layer" && <Badge tone="amber">Digitalizada · camada OCR</Badge>}
          {analysis?.mode === "mixed" && <Badge tone="slate">Texto + imagem</Badge>}
          {(analysis?.fields?.length ?? 0) > 0 && <Badge tone="slate">Formulário</Badge>}
          {ops.length > 0 && <Badge tone="green">{ops.length} alteração(ões)</Badge>}
        </span>
      </div>
      <div
        ref={ref}
        className={`relative bg-white shadow-page select-none ${cursor}`}
        style={{ width: W, height: H }}
        onMouseDown={onBackgroundClick}
        data-testid={`page-${page.number}`}
      >
        <img
          src={p.imageUrl}
          alt={`Página ${page.number + 1}`}
          draggable={false}
          className="pointer-events-none absolute inset-0 h-full w-full"
        />
        {(p.previewLoading || p.analysisLoading) && (
          <div className="pointer-events-none absolute right-2 top-2 z-40 flex items-center gap-1.5 rounded-full bg-night-950/85 px-2.5 py-1 text-xs text-ink-900">
            <Spinner className="h-3.5 w-3.5" />
            {p.analysisLoading ? (analysis ? "Analisando…" : "Reconhecendo texto…") : "Aplicando alteração…"}
          </div>
        )}
        {p.analysisError && (
          <div className="absolute inset-x-4 top-4 z-40 rounded-md bg-red-500/10 px-3 py-2 text-sm text-red-300 ring-1 ring-red-500/30">
            {p.analysisError}
          </div>
        )}

        {/* elementos de texto existentes */}
        {analysis?.elements.map((el) => {
          const op = elementOps.get(el.id);
          if (op?.type === "delete_element") return null;
          if (editingElement?.id === el.id) return null;
          const [x0, y0, x1, y1] = el.bbox;
          const extra = op?.type === "edit_text" && !el.multiline ? Math.max(0, measureText(op.text, op.size ?? el.size, el) - (x1 - x0)) : 0;
          const left = op?.type === "edit_text" && el.align === "right" ? x0 - extra : el.align === "center" ? x0 - extra / 2 : x0;
          return (
            <div
              key={el.id}
              data-el={el.id}
              aria-label={el.text}
              role="button"
              title={el.kind === "ocr" ? "Texto reconhecido por OCR — clique para editar" : el.multiline ? "Parágrafo — clique para editar" : "Clique para editar"}
              className={`el-box absolute rounded-[2px] ${op ? "edited" : ""} ${p.tool === "edit" ? "cursor-text" : "pointer-events-none"}`}
              style={{
                // textos ficam acima das imagens; área mínima de clique para textos pequenos
                left: left * zoom,
                top: y0 * zoom - Math.max(0, (12 - (y1 - y0) * zoom) / 2),
                width: Math.max(8, (x1 - x0 + extra) * zoom),
                height: Math.max(12, (y1 - y0) * zoom),
                zIndex: 3,
              }}
              onMouseDown={(e) => {
                if (p.tool !== "edit") return;
                e.stopPropagation();
                e.preventDefault();
                p.onActivate({ kind: "element", page: page.number, elementId: el.id });
              }}
            />
          );
        })}

        {/* imagens existentes (selecionáveis para exclusão) */}
        {p.tool === "edit" &&
          analysis?.images.map((im) => {
            if (elementOps.get(im.id)) return null;
            const [x0, y0, x1, y1] = im.bbox;
            const sel = p.selected?.kind === "image" && p.selected.id === im.id;
            return (
              <div
                key={im.id}
                className={`absolute ${sel ? "ring-2 ring-brand-500" : "hover:ring-1 hover:ring-brand-500/60"}`}
                style={{ left: x0 * zoom, top: y0 * zoom, width: (x1 - x0) * zoom, height: (y1 - y0) * zoom, zIndex: 1 }}
                onMouseDown={(e) => {
                  if (e.target !== e.currentTarget) return;
                  e.stopPropagation();
                  p.onSelect({ kind: "image", page: page.number, id: im.id });
                }}
              >
                {sel && <SelectionTools onDelete={() => p.onDeleteElement(page.number, im.id)} label="Imagem do documento" />}
              </div>
            );
          })}

        {/* campos de formulário */}
        {analysis?.fields?.map((f) => {
          const op = elementOps.get(f.id);
          const value = op?.type === "set_field" ? op.value : f.value;
          const [x0, y0, x1, y1] = f.bbox;
          const box: React.CSSProperties = {
            left: x0 * zoom, top: y0 * zoom, width: (x1 - x0) * zoom, height: (y1 - y0) * zoom, zIndex: 4,
            fontSize: Math.max(8, (f.size || 10) * zoom),
          };
          const title = `Campo de formulário: ${f.name}${f.readOnly ? " (somente leitura)" : ""}`;
          if (f.type === "checkbox" || f.type === "radio") {
            const checked = typeof value === "boolean" ? value : value !== "Off" && value !== "";
            return (
              <label key={f.id} title={title} className="absolute grid cursor-pointer place-items-center rounded-sm bg-sky-200/30 ring-1 ring-sky-400/60"
                     style={box} onMouseDown={(e) => e.stopPropagation()}>
                <input type="checkbox" className="h-3/4 w-3/4 cursor-pointer opacity-0" checked={checked} disabled={f.readOnly}
                       onChange={(e) => p.onSetField(page.number, f, e.target.checked)} aria-label={f.name} />
              </label>
            );
          }
          if (f.type === "choice") {
            return (
              <select key={f.id} title={title} disabled={f.readOnly} aria-label={f.name}
                      className="absolute cursor-pointer rounded-sm bg-sky-200/30 text-transparent ring-1 ring-sky-400/60 focus:bg-white focus:text-black"
                      style={box} value={String(value)} onMouseDown={(e) => e.stopPropagation()}
                      onChange={(e) => p.onSetField(page.number, f, e.target.value)}>
                {(f.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            );
          }
          return (
            <input key={f.id + String(value)} title={title} disabled={f.readOnly} aria-label={f.name} defaultValue={String(value)}
                   className="absolute rounded-sm bg-sky-200/30 px-1 text-transparent caret-black ring-1 ring-sky-400/60 focus:bg-white focus:text-black focus:outline-none focus:ring-2 focus:ring-brand-500"
                   style={box} onMouseDown={(e) => e.stopPropagation()}
                   onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); e.stopPropagation(); }}
                   onBlur={(e) => { if (e.target.value !== String(value)) p.onSetField(page.number, f, e.target.value); }} />
          );
        })}

        {/* textos e imagens adicionados */}
        {ops.map((op) => {
          if (op.type === "add_text") {
            const id = op.localId!;
            if (editingAdded && editingAdded.type === "add_text" && editingAdded.localId === id) return null;
            const d = drag?.id === id ? drag : null;
            const w = measureText(op.text, op.size, { bold: op.bold, italic: op.italic, serif: op.fontFamily === "serif", mono: op.fontFamily === "mono" });
            const h = op.text.split("\n").length * op.size * 1.2;
            const sel = p.selected?.kind === "addedText" && p.selected.id === id;
            return (
              <div
                key={id}
                data-added={id}
                className={`absolute cursor-move rounded-sm ${sel || d ? "ring-2 ring-brand-500" : "ring-1 ring-dashed ring-brand-500/0 hover:ring-brand-500/70"}`}
                style={{ left: (op.x + (d?.dx ?? 0)) * zoom - 2, top: (op.y + (d?.dy ?? 0)) * zoom - 2, width: w * zoom + 4, height: h * zoom + 4, zIndex: 5,
                         background: d ? "rgba(47,107,255,.08)" : undefined }}
                onMouseDown={(e) =>
                  startDrag(e, id, "move", () => {
                    p.onSelect({ kind: "addedText", page: page.number, id });
                    p.onActivate({ kind: "added", page: page.number, localId: id });
                  })
                }
              />
            );
          }
          if (op.type === "add_image") {
            const id = op.localId!;
            const d = drag?.id === id ? drag : null;
            const [x0, y0, x1, y1] = op.rect;
            const sel = p.selected?.kind === "addedImage" && p.selected.id === id;
            const w = Math.max(8, x1 - x0 + (d?.dw ?? 0));
            const hh = Math.max(8, y1 - y0 + (d?.dh ?? 0));
            return (
              <div
                key={id}
                className={`absolute cursor-move ${sel ? "ring-2 ring-brand-500" : "hover:ring-1 hover:ring-brand-500/70"}`}
                style={{ left: (x0 + (d?.dx ?? 0)) * zoom, top: (y0 + (d?.dy ?? 0)) * zoom, width: w * zoom, height: hh * zoom, zIndex: 6 }}
                onMouseDown={(e) => startDrag(e, id, "move", () => p.onSelect({ kind: "addedImage", page: page.number, id }))}
              >
                {d && op.previewUrl && <img src={op.previewUrl} className="h-full w-full opacity-70" draggable={false} alt="" />}
                {sel && (
                  <>
                    <div
                      className="absolute -bottom-1.5 -right-1.5 h-3 w-3 cursor-nwse-resize rounded-sm border-2 border-white bg-brand-500"
                      onMouseDown={(e) => startDrag(e, id, "resize")}
                    />
                    <SelectionTools onDelete={() => p.onRemoveAdded(id)} label="Imagem adicionada" />
                  </>
                )}
              </div>
            );
          }
          return null;
        })}

        {editingElement && (
          <InlineEditor
            key={editingElement.id}
            zoom={zoom}
            pageWidth={W}
            box={editingElement.bbox}
            maxExtent={editingElement.maxExtent}
            multiline={editingElement.multiline}
            lineHeight={editingElement.lineHeight}
            cover={editingElement.bgColor ?? "#ffffff"}
            kindLabel={editingElement.kind === "ocr" ? "OCR · reconstrução visual" : undefined}
            initial={draftFor(editingElement, elementOps.get(editingElement.id))}
            onCommit={(d) => p.onCommitElement(editingElement, d)}
            onCancel={() => p.onActivate(null)}
            onDelete={() => p.onDeleteElement(page.number, editingElement.id)}
          />
        )}
        {editingAdded && editingAdded.type === "add_text" && (
          <InlineEditor
            key={editingAdded.localId}
            zoom={zoom}
            pageWidth={W}
            box={[editingAdded.x, editingAdded.y, editingAdded.x + Math.max(40, measureText(editingAdded.text, editingAdded.size, {})), editingAdded.y + editingAdded.size * 1.2]}
            maxExtent={0}
            cover={null}
            kindLabel="Texto novo"
            initial={{
              text: editingAdded.text,
              size: editingAdded.size,
              color: editingAdded.color,
              bold: !!editingAdded.bold,
              italic: !!editingAdded.italic,
              align: "left",
              serif: editingAdded.fontFamily === "serif",
              mono: editingAdded.fontFamily === "mono",
            }}
            onCommit={(d) => p.onCommitAdded(editingAdded.localId!, d)}
            onCancel={() => p.onCommitAdded(editingAdded.localId!, { ...draftOfAdded(editingAdded) })}
            onDelete={() => p.onRemoveAdded(editingAdded.localId!)}
          />
        )}
      </div>
      {analysis?.notes.length ? (
        <ul className="mt-2 space-y-1 text-xs text-ink-600">
          {analysis.notes.map((n, i) => (
            <li key={i} className="flex gap-1.5">
              <span className="text-amber-500">●</span>
              {n}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function draftOfAdded(op: Extract<Operation, { type: "add_text" }>): Draft {
  return { text: op.text, size: op.size, color: op.color, bold: !!op.bold, italic: !!op.italic, align: "left",
           serif: op.fontFamily === "serif", mono: op.fontFamily === "mono" };
}

function draftFor(el: TextElement, op: Operation | undefined): Draft {
  const e = op?.type === "edit_text" ? op : undefined;
  return {
    text: e?.text ?? el.text,
    size: e?.size ?? el.size,
    color: e?.color ?? el.color,
    bold: e?.bold ?? el.bold,
    italic: e?.italic ?? el.italic,
    align: e?.align ?? el.align,
    serif: el.serif,
    mono: el.mono,
  };
}

function Badge({ children, tone }: { children: React.ReactNode; tone: "amber" | "green" | "slate" }) {
  const cls = { amber: "bg-amber-500/10 text-amber-300", green: "bg-emerald-500/10 text-emerald-300", slate: "bg-ink-100 text-ink-600" }[tone];
  return <span className={`rounded-full px-2 py-0.5 font-medium ${cls}`}>{children}</span>;
}

function SelectionTools({ onDelete, label }: { onDelete: () => void; label: string }) {
  return (
    <div className="absolute -top-9 left-0 z-30 flex items-center gap-1 whitespace-nowrap rounded-lg border border-ink-200 bg-night-800 px-2 py-1 text-xs shadow"
         onMouseDown={(e) => e.stopPropagation()}>
      <span className="text-ink-400">{label}</span>
      <button className="rounded px-1.5 py-0.5 text-red-400 hover:bg-red-500/100/15" onClick={onDelete}>
        Excluir
      </button>
    </div>
  );
}
