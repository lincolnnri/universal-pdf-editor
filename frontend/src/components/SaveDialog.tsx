import { useMemo, useState } from "react";
import type { ExportReport } from "../lib/api";
import { Spinner } from "./UploadScreen";

export type SaveState =
  | { phase: "closed" }
  | { phase: "choose-pages" }
  | { phase: "confirm-signature" }
  | { phase: "working" }
  | { phase: "done"; report: ExportReport }
  | { phase: "error"; message: string; report?: ExportReport };

interface Props {
  state: SaveState;
  onClose: () => void;
  onRetry: () => void;
  onConfirmSignature: () => void;
  pageCount: number;
  editedPages: number[];
  hasEdits: boolean;
  thumbUrl: (page: number) => string;
  onChoosePages: (pages: number[] | null) => void;
}

/** "1-3, 5, 8-10" → [0,1,2,4,7,8,9] (base 0). Retorna null se inválido. */
export function parseRanges(text: string, count: number): number[] | null {
  const out = new Set<number>();
  const parts = text.split(/[,;\s]+/).filter(Boolean);
  if (!parts.length) return null;
  for (const part of parts) {
    const m = part.match(/^(\d+)(?:\s*[-–]\s*(\d+))?$/);
    if (!m) return null;
    const a = parseInt(m[1], 10);
    const b = m[2] ? parseInt(m[2], 10) : a;
    if (a < 1 || b < 1 || a > count || b > count) return null;
    for (let i = Math.min(a, b); i <= Math.max(a, b); i++) out.add(i - 1);
  }
  return Array.from(out).sort((x, y) => x - y);
}

export function formatRanges(pages: number[]): string {
  const n = [...pages].sort((a, b) => a - b).map((p) => p + 1);
  const out: string[] = [];
  for (let i = 0; i < n.length; ) {
    let j = i;
    while (j + 1 < n.length && n[j + 1] === n[j] + 1) j++;
    out.push(i === j ? `${n[i]}` : `${n[i]}-${n[j]}`);
    i = j + 1;
  }
  return out.join(", ");
}

export function SaveDialog(props: Props) {
  const { state, onClose, onRetry, onConfirmSignature } = props;
  if (state.phase === "closed") return null;
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm" role="dialog" aria-modal>
      <div className={`w-full rounded-2xl bg-night-900 p-6 shadow-2xl ring-1 ring-brand-500/20 ${state.phase === "choose-pages" ? "max-w-2xl" : "max-w-lg"}`}>
        {state.phase === "choose-pages" && <PagePicker {...props} />}
        {state.phase === "confirm-signature" && (
          <>
            <h2 className="text-lg font-semibold">Documento com assinatura digital</h2>
            <p className="mt-2 text-ink-600">
              Este PDF possui assinatura digital. Ao salvar as alterações, a assinatura deixará de ser válida e o
              arquivo gerado <b>não</b> poderá ser apresentado como a versão original assinada.
            </p>
            <div className="mt-6 flex justify-end gap-2">
              <button className="rounded-lg px-4 py-2 text-ink-600 hover:bg-ink-100" onClick={onClose}>
                Cancelar
              </button>
              <button className="rounded-lg bg-brand-500 px-4 py-2 font-medium text-night-950 hover:bg-brand-600" onClick={onConfirmSignature}>
                Entendo, gerar PDF editado
              </button>
            </div>
          </>
        )}
        {state.phase === "working" && (
          <div className="flex items-start gap-4">
            <Spinner className="mt-1 h-6 w-6 text-brand-500" />
            <div>
              <h2 className="text-lg font-semibold">Gerando e verificando o PDF…</h2>
              <p className="mt-1 text-ink-600 text-sm">
                Aplicando as alterações ao arquivo, reabrindo o PDF gerado e conferindo cada alteração e as áreas
                não editadas. O download começa somente após a verificação.
              </p>
            </div>
          </div>
        )}
        {state.phase === "done" && (
          <>
            <div className="flex items-center gap-3">
              <span className="grid h-10 w-10 place-items-center rounded-full bg-emerald-500/100/15 text-emerald-400">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M5 12l5 5 9-10" /></svg>
              </span>
              <div>
                <h2 className="text-lg font-semibold">PDF salvo e verificado</h2>
                <p className="text-sm text-ink-600">
                  {state.report.checks.filter((c) => c.ok).length} verificações aprovadas ·{" "}
                  {(state.report.sizeBytes / 1024).toFixed(0)} KB · {state.report.durationMs} ms
                </p>
              </div>
            </div>
            <Warnings items={state.report.warnings} />
            <Checks report={state.report} />
            <div className="mt-6 flex justify-end gap-2">
              <button className="rounded-lg px-4 py-2 text-ink-600 hover:bg-ink-100" onClick={onClose}>
                Fechar
              </button>
              {state.report.downloadUrl && (
                <a
                  href={state.report.downloadUrl}
                  download={state.report.filename}
                  className="rounded-lg bg-brand-500 px-4 py-2 font-medium text-night-950 hover:bg-brand-600"
                >
                  Baixar novamente
                </a>
              )}
            </div>
          </>
        )}
        {state.phase === "error" && (
          <>
            <div className="flex items-center gap-3">
              <span className="grid h-10 w-10 place-items-center rounded-full bg-red-500/15 text-red-400 font-bold">!</span>
              <div>
                <h2 className="text-lg font-semibold">O PDF não foi salvo</h2>
                <p className="text-sm text-ink-600">Nenhum arquivo foi baixado.</p>
              </div>
            </div>
            <div className="mt-4 rounded-lg bg-red-500/10 px-3 py-2 text-sm text-red-300" role="alert">
              {state.message}
            </div>
            {state.report && (
              <>
                {state.report.errors.length > 0 && (
                  <ul className="mt-3 list-disc pl-5 text-sm text-red-300">
                    {state.report.errors.map((e, i) => (
                      <li key={i}>{e}</li>
                    ))}
                  </ul>
                )}
                <Checks report={state.report} />
              </>
            )}
            <div className="mt-6 flex justify-end gap-2">
              <button className="rounded-lg px-4 py-2 text-ink-600 hover:bg-ink-100" onClick={onClose}>
                Fechar
              </button>
              <button className="rounded-lg bg-brand-500 px-4 py-2 font-medium text-night-950 hover:bg-brand-600" onClick={onRetry}>
                Tentar novamente
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function Warnings({ items }: { items: string[] }) {
  const unique = Array.from(new Set(items));
  if (!unique.length) return null;
  return (
    <ul className="mt-4 space-y-1 rounded-lg bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
      {unique.map((w, i) => (
        <li key={i}>• {w}</li>
      ))}
    </ul>
  );
}

function Checks({ report }: { report: ExportReport }) {
  return (
    <details className="mt-4 text-sm">
      <summary className="cursor-pointer text-ink-600 hover:text-ink-900">Detalhes da verificação</summary>
      <ul className="mt-2 max-h-56 space-y-1 overflow-auto thin-scroll">
        {report.checks.map((c, i) => (
          <li key={i} className="flex gap-2">
            <span className={c.ok ? "text-emerald-400" : "text-red-400"}>{c.ok ? "✓" : "✗"}</span>
            <span>
              {c.name}
              {c.detail && <span className="text-ink-400"> — {c.detail}</span>}
            </span>
          </li>
        ))}
      </ul>
    </details>
  );
}

function PagePicker({ pageCount, editedPages, hasEdits, thumbUrl, onChoosePages, onClose }: Props) {
  const [mode, setMode] = useState<"all" | "some">(hasEdits ? "all" : "some");
  const [sel, setSel] = useState<Set<number>>(() => new Set(hasEdits ? editedPages : []));
  const [text, setText] = useState(() => formatRanges(hasEdits ? editedPages : []));
  const [textError, setTextError] = useState(false);
  const selected = useMemo(() => Array.from(sel).sort((a, b) => a - b), [sel]);

  const setFromSet = (next: Set<number>) => {
    setSel(next);
    setText(formatRanges(Array.from(next)));
    setTextError(false);
  };
  const toggle = (p: number) => {
    const next = new Set(sel);
    if (next.has(p)) next.delete(p);
    else next.add(p);
    setFromSet(next);
  };
  const onText = (v: string) => {
    setText(v);
    const parsed = parseRanges(v, pageCount);
    setTextError(v.trim() !== "" && parsed === null);
    if (parsed) setSel(new Set(parsed));
    else if (v.trim() === "") setSel(new Set());
  };
  const canDownload = mode === "all" ? hasEdits : selected.length > 0 && !textError;
  const omittedEdits = mode === "some" ? editedPages.filter((p) => !sel.has(p)) : [];

  return (
    <div data-testid="page-picker">
      <h2 className="text-lg font-semibold">Quais páginas você quer baixar?</h2>
      <div className="mt-4 space-y-2">
        <label className={`flex cursor-pointer items-center gap-3 rounded-lg border px-3 py-2.5 ${mode === "all" ? "border-brand-500 bg-brand-50" : "border-ink-200"} ${hasEdits ? "" : "opacity-50"}`}>
          <input type="radio" name="pmode" checked={mode === "all"} disabled={!hasEdits} onChange={() => setMode("all")} data-testid="pages-all" />
          <span>
            Todas as páginas <span className="text-ink-400">({pageCount})</span>
            {!hasEdits && <span className="block text-xs text-ink-400">Sem alterações: escolha as páginas que deseja extrair.</span>}
          </span>
        </label>
        <label className={`flex cursor-pointer items-center gap-3 rounded-lg border px-3 py-2.5 ${mode === "some" ? "border-brand-500 bg-brand-50" : "border-ink-200"}`}>
          <input type="radio" name="pmode" checked={mode === "some"} onChange={() => setMode("some")} data-testid="pages-some" />
          <span>Escolher páginas</span>
        </label>
      </div>

      {mode === "some" && (
        <div className="mt-4">
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={text}
              onChange={(e) => onText(e.target.value)}
              placeholder="Ex.: 1-3, 5, 8"
              className={`min-w-0 flex-1 rounded-lg border px-3 py-2 text-sm outline-none focus:ring-2 ${textError ? "border-red-400 focus:ring-red-500/30" : "border-ink-200 focus:ring-brand-500/40"}`}
              data-testid="pages-input"
              aria-label="Páginas"
            />
            <button className="rounded-lg px-3 py-2 text-sm text-ink-600 hover:bg-ink-100" onClick={() => setFromSet(new Set(Array.from({ length: pageCount }, (_, i) => i)))}>
              Todas
            </button>
            <button className="rounded-lg px-3 py-2 text-sm text-ink-600 hover:bg-ink-100" onClick={() => setFromSet(new Set())}>
              Nenhuma
            </button>
          </div>
          {textError && <p className="mt-1 text-xs text-red-400">Use números de 1 a {pageCount}, separados por vírgula ou com intervalo (ex.: 2-4).</p>}
          <div className="thin-scroll mt-3 grid max-h-72 grid-cols-4 gap-2 overflow-y-auto sm:grid-cols-6">
            {Array.from({ length: pageCount }, (_, p) => (
              <button
                key={p}
                onClick={() => toggle(p)}
                className={`relative rounded-lg p-1 text-left ${sel.has(p) ? "bg-brand-50 ring-2 ring-brand-500" : "ring-1 ring-ink-200 hover:ring-ink-400"}`}
                data-testid={`pick-${p + 1}`}
                aria-pressed={sel.has(p)}
              >
                <img src={thumbUrl(p)} loading="lazy" alt="" className={`w-full bg-white ${sel.has(p) ? "" : "opacity-60"}`} />
                <span className="mt-0.5 flex items-center justify-between text-xs text-ink-600">
                  {p + 1}
                  {editedPages.includes(p) && <span className="h-2 w-2 rounded-full bg-emerald-500/100" title="Página editada" />}
                </span>
                {sel.has(p) && (
                  <span className="absolute right-1.5 top-1.5 grid h-5 w-5 place-items-center rounded-full bg-brand-500 text-night-950">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3"><path d="M5 12l5 5 9-10" /></svg>
                  </span>
                )}
              </button>
            ))}
          </div>
          <p className="mt-2 text-sm text-ink-600">
            {selected.length} de {pageCount} página(s) selecionada(s)
          </p>
          {omittedEdits.length > 0 && (
            <p className="mt-1 text-xs text-amber-300">
              Atenção: as alterações nas páginas {formatRanges(omittedEdits)} não estarão no arquivo, porque essas páginas não foram selecionadas.
            </p>
          )}
        </div>
      )}

      <div className="mt-6 flex justify-end gap-2">
        <button className="rounded-lg px-4 py-2 text-ink-600 hover:bg-ink-100" onClick={onClose}>
          Cancelar
        </button>
        <button
          className="rounded-lg bg-brand-500 px-4 py-2 font-medium text-night-950 hover:bg-brand-600 disabled:opacity-50"
          disabled={!canDownload}
          onClick={() => onChoosePages(mode === "all" || selected.length === pageCount ? null : selected)}
          data-testid="pages-confirm"
        >
          Baixar PDF
        </button>
      </div>
    </div>
  );
}
