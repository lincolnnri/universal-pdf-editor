import { useCallback, useRef, useState } from "react";
import { api, ApiError, type DocInfo } from "../lib/api";
import { Logo } from "./Logo";

export function UploadScreen({ onLoaded }: { onLoaded: (doc: DocInfo) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<File | null>(null);
  const [password, setPassword] = useState("");
  const [needsPassword, setNeedsPassword] = useState(false);

  const send = useCallback(
    async (file: File, pw?: string) => {
      setError(null);
      if (!file.name.toLowerCase().endsWith(".pdf") && file.type !== "application/pdf") {
        setError("Selecione um arquivo PDF.");
        return;
      }
      setBusy(true);
      try {
        const doc = await api.upload(file, pw);
        onLoaded(doc);
      } catch (e) {
        if (e instanceof ApiError && (e.code === "password_required" || e.code === "wrong_password")) {
          setPending(file);
          setNeedsPassword(true);
          setError(e.code === "wrong_password" ? "Senha incorreta. Tente novamente." : null);
        } else {
          setError(e instanceof Error ? e.message : "Falha ao carregar o arquivo.");
        }
      } finally {
        setBusy(false);
      }
    },
    [onLoaded],
  );

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files?.[0];
    if (f) void send(f);
  };

  return (
    <div
      className="min-h-full flex flex-col"
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={(e) => {
        if (e.currentTarget === e.target) setDragging(false);
      }}
      onDrop={onDrop}
    >
      <header className="px-8 py-6">
        <Logo />
      </header>
      <main className="flex-1 flex items-center justify-center px-6 pb-24">
        <div className="w-full max-w-2xl text-center">
          <h1 className="text-4xl font-semibold tracking-tight text-ink-900">Edite PDFs clicando direto no texto.</h1>
          <p className="mt-3 text-ink-600 text-lg">
            Carregue, clique, edite, salve e baixe. O PDF gerado é verificado antes do download.
          </p>

          <div
            className={`mt-10 rounded-2xl border-2 border-dashed px-8 py-14 transition-colors ${
              dragging ? "border-brand-500 bg-brand-50" : "border-ink-200 bg-white"
            }`}
          >
            {needsPassword && pending ? (
              <form
                className="mx-auto max-w-sm text-left"
                onSubmit={(e) => {
                  e.preventDefault();
                  void send(pending, password);
                }}
              >
                <p className="font-medium text-ink-800">“{pending.name}” é protegido por senha.</p>
                <p className="text-sm text-ink-600 mt-1">
                  Informe a senha do documento. Ela é usada apenas para abri-lo nesta sessão.
                </p>
                <input
                  autoFocus
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="mt-4 w-full rounded-lg border border-ink-200 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-brand-500"
                  placeholder="Senha"
                />
                <div className="mt-4 flex gap-2">
                  <button
                    type="submit"
                    disabled={busy || !password}
                    className="rounded-lg bg-brand-500 px-4 py-2 font-medium text-white hover:bg-brand-600 disabled:opacity-50"
                  >
                    {busy ? "Abrindo…" : "Abrir"}
                  </button>
                  <button
                    type="button"
                    className="rounded-lg px-4 py-2 text-ink-600 hover:bg-ink-100"
                    onClick={() => {
                      setNeedsPassword(false);
                      setPending(null);
                      setPassword("");
                      setError(null);
                    }}
                  >
                    Cancelar
                  </button>
                </div>
              </form>
            ) : (
              <>
                <button
                  onClick={() => input.current?.click()}
                  disabled={busy}
                  className="inline-flex items-center gap-2 rounded-xl bg-brand-500 px-8 py-4 text-lg font-semibold text-white shadow-sm hover:bg-brand-600 disabled:opacity-60"
                >
                  {busy ? (
                    <>
                      <Spinner /> Carregando…
                    </>
                  ) : (
                    <>
                      <UploadIcon /> Carregar PDF
                    </>
                  )}
                </button>
                <p className="mt-4 text-ink-400">ou arraste e solte o arquivo aqui</p>
              </>
            )}
            <input
              ref={input}
              type="file"
              accept="application/pdf,.pdf"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void send(f);
                e.target.value = "";
              }}
            />
          </div>
          {error && (
            <div role="alert" className="mt-4 rounded-lg bg-red-50 px-4 py-3 text-red-700">
              {error}
            </div>
          )}
          <p className="mt-8 text-sm text-ink-400">
            Os arquivos ficam em uma área temporária isolada e são apagados ao fechar o documento ou após o tempo
            de inatividade.
          </p>
        </div>
      </main>
    </div>
  );
}

export function Spinner({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg className={`animate-spin ${className}`} viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity=".25" strokeWidth="3" />
      <path d="M22 12a10 10 0 0 1-10 10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

function UploadIcon() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M12 16V4M6 10l6-6 6 6M4 20h16" />
    </svg>
  );
}
