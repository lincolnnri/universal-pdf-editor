import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Align } from "../lib/api";
import { cssFamily, measureText } from "../lib/text";

export interface Draft {
  text: string;
  size: number;
  color: string;
  bold: boolean;
  italic: boolean;
  align: Align;
  serif: boolean;
  mono: boolean;
}

interface Props {
  initial: Draft;
  zoom: number;
  /** caixa original do texto em pontos (display) */
  box: [number, number, number, number];
  /** espaço máximo disponível ao longo da linha (pt); 0 = livre */
  maxExtent: number;
  /** cor para cobrir o texto original na imagem enquanto edita */
  cover: string | null;
  kindLabel?: string;
  /** largura da página em px (para manter a barra de formatação visível) */
  pageWidth: number;
  /** parágrafo: caixa com largura fixa e quebra automática */
  multiline?: boolean;
  lineHeight?: number | null;
  onCommit: (d: Draft) => void;
  onCancel: () => void;
  onDelete: () => void;
}

export function InlineEditor({ initial, zoom, box, maxExtent, cover, kindLabel, pageWidth, multiline, lineHeight, onCommit, onCancel, onDelete }: Props) {
  const [draft, setDraft] = useState<Draft>(initial);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const root = useRef<HTMLDivElement>(null);
  const editable = useRef<HTMLDivElement>(null);
  const done = useRef(false);

  const commit = () => {
    if (done.current) return;
    done.current = true;
    const text = (editable.current?.innerText ?? draftRef.current.text).replace(/ /g, " ").replace(/\n$/, "");
    onCommit({ ...draftRef.current, text });
  };

  useLayoutEffect(() => {
    const el = editable.current;
    if (!el) return;
    el.innerText = initial.text;
    el.focus();
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) commit();
    };
    document.addEventListener("mousedown", onDown, true);
    return () => document.removeEventListener("mousedown", onDown, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [x0, y0, x1, y1] = box;
  const h = y1 - y0;
  const [liveText, setLiveText] = useState(initial.text);
  const estimated = measureText(liveText, draft.size, draft);
  const overflow = !multiline && maxExtent > 0 && estimated > maxExtent * 1.02;

  // posição: ancorada conforme o alinhamento detectado no documento
  const style: React.CSSProperties = {
    position: "absolute",
    top: y0 * zoom,
    minHeight: h * zoom,
    fontSize: draft.size * zoom,
    lineHeight: `${h * zoom}px`,
    fontFamily: cssFamily(draft.serif, draft.mono),
    fontWeight: draft.bold ? 700 : 400,
    fontStyle: draft.italic ? "italic" : "normal",
    color: draft.color,
    textAlign: draft.align,
    minWidth: Math.max(8, (x1 - x0) * zoom),
  };
  if (multiline) {
    style.left = x0 * zoom;
    style.width = (x1 - x0) * zoom;
    style.whiteSpace = "pre-wrap";
    style.lineHeight = `${(lineHeight ?? draft.size * 1.2) * zoom * (draft.size / initial.size)}px`;
    style.top = (y0 - ((lineHeight ?? draft.size * 1.2) - draft.size) / 2) * zoom;
  } else if (draft.align === "right") style.right = `calc(100% - ${x1 * zoom}px)`;
  else if (draft.align === "center") {
    style.left = ((x0 + x1) / 2) * zoom;
    style.transform = "translateX(-50%)";
  } else style.left = x0 * zoom;

  const stop = (e: React.MouseEvent) => e.preventDefault();
  const set = (patch: Partial<Draft>) => setDraft((d) => ({ ...d, ...patch }));

  return (
    <div ref={root} className="contents">
      {cover && (
        <div
          className="absolute pointer-events-none"
          style={{ left: x0 * zoom - 1, top: y0 * zoom - 1, width: (x1 - x0) * zoom + 2, height: h * zoom + 2, background: cover }}
        />
      )}
      <div
        ref={editable}
        contentEditable
        suppressContentEditableWarning
        spellCheck={false}
        data-testid="inline-editor"
        dir="auto"
        className="inline-editor z-20 rounded-[2px] ring-2 ring-brand-500/70 bg-white/0"
        style={style}
        onInput={(e) => setLiveText((e.target as HTMLDivElement).innerText)}
        onPaste={(e) => {
          e.preventDefault();
          document.execCommand("insertText", false, e.clipboardData.getData("text/plain"));
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            commit();
          } else if (e.key === "Enter" && e.shiftKey) {
            e.preventDefault();
            document.execCommand("insertLineBreak");
          } else if (e.key === "Escape") {
            e.preventDefault();
            done.current = true;
            onCancel();
          }
          e.stopPropagation();
        }}
      />
      {/* barra de formatação */}
      <div
        className="absolute z-30 flex items-center gap-1 rounded-lg border border-ink-200 bg-night-800 px-1.5 py-1 shadow-lg text-sm"
        style={
          x0 * zoom > pageWidth / 2
            ? { right: Math.max(0, pageWidth - x1 * zoom), top: Math.max(0, y0 * zoom - 44) }
            : { left: Math.max(0, x0 * zoom), top: Math.max(0, y0 * zoom - 44) }
        }
        onMouseDown={(e) => e.stopPropagation()}
      >
        {kindLabel && <span className="px-1.5 text-xs text-ink-400 whitespace-nowrap">{kindLabel}</span>}
        <label className="flex items-center gap-1 px-1" title="Tamanho da fonte (pt)">
          <input
            type="number"
            min={3}
            max={200}
            step={0.5}
            value={Number(draft.size.toFixed(1))}
            onChange={(e) => set({ size: Math.max(3, Number(e.target.value) || draft.size) })}
            className="w-14 rounded border border-ink-200 px-1 py-0.5 text-sm"
            aria-label="Tamanho da fonte"
          />
          <span className="text-ink-400 text-xs">pt</span>
        </label>
        <input
          type="color"
          value={draft.color}
          onChange={(e) => set({ color: e.target.value })}
          className="h-7 w-7 cursor-pointer rounded border border-ink-200 p-0.5"
          title="Cor do texto"
          aria-label="Cor do texto"
        />
        <ToolBtn active={draft.bold} onMouseDown={stop} onClick={() => set({ bold: !draft.bold })} title="Negrito">
          <b>B</b>
        </ToolBtn>
        <ToolBtn active={draft.italic} onMouseDown={stop} onClick={() => set({ italic: !draft.italic })} title="Itálico">
          <i className="font-serif">I</i>
        </ToolBtn>
        <span className="mx-0.5 h-5 w-px bg-ink-200" />
        {((multiline ? ["left", "center", "right", "justify"] : ["left", "center", "right"]) as Align[]).map((a) => (
          <ToolBtn key={a} active={draft.align === a} onMouseDown={stop} onClick={() => set({ align: a })}
                   title={{ left: "Alinhar à esquerda", center: "Centralizar", right: "Alinhar à direita", justify: "Justificar" }[a]}>
            <AlignIcon a={a} />
          </ToolBtn>
        ))}
        <span className="mx-0.5 h-5 w-px bg-ink-200" />
        <ToolBtn onMouseDown={stop} onClick={() => { done.current = true; onDelete(); }} title="Excluir texto">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" /></svg>
        </ToolBtn>
        <button
          onMouseDown={stop}
          onClick={commit}
          className="ml-0.5 rounded-md bg-brand-500 px-2.5 py-1 font-medium text-night-950 hover:bg-brand-600"
          data-testid="commit-edit"
        >
          Concluir
        </button>
      </div>
      {overflow && (
        <div
          className="absolute z-30 rounded-md bg-amber-500/10 px-2 py-1 text-xs text-amber-200 shadow ring-1 ring-amber-500/30"
          style={{ left: x0 * zoom, top: (y1 + 4) * zoom }}
        >
          O texto é maior que o espaço disponível — será comprimido para não invadir elementos vizinhos.
        </div>
      )}
    </div>
  );
}

function ToolBtn(props: React.ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean }) {
  const { active, className, ...rest } = props;
  return (
    <button
      {...rest}
      className={`grid h-7 w-7 place-items-center rounded-md ${active ? "bg-brand-100 text-brand-700" : "text-ink-600 hover:bg-ink-100"} ${className ?? ""}`}
    />
  );
}

function AlignIcon({ a }: { a: Align }) {
  const lines = a === "left" ? [[4, 20], [4, 14], [4, 18]] : a === "right" ? [[4, 20], [10, 20], [6, 20]]
    : a === "justify" ? [[4, 20], [4, 20], [4, 20]] : [[4, 20], [7, 17], [5, 19]];
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      {lines.map(([a1, b1], i) => (
        <line key={i} x1={a1} x2={b1} y1={7 + i * 5} y2={7 + i * 5} />
      ))}
    </svg>
  );
}
