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
      <NightScene />
      <header className="relative z-10 px-8 py-6">
        <Logo />
      </header>
      <main className="relative z-10 flex-1 flex items-center justify-center px-6 pb-40">
        <div className="w-full max-w-2xl text-center">
          <p className="font-display text-xs font-bold uppercase tracking-[0.45em] text-brand-500/80">Editor de PDF</p>
          <h1 className="mt-3 font-display text-4xl font-bold leading-tight tracking-wide text-ink-900 sm:text-5xl">
            Edite PDFs clicando direto no <span className="text-brand-500 drop-shadow-[0_0_18px_rgba(242,182,50,.45)]">texto</span>.
          </h1>
          <p className="mt-4 text-ink-600 text-lg">
            Carregue, clique, edite, salve e baixe. O PDF gerado é verificado antes do download.
          </p>

          <div
            className={`mt-10 rounded-2xl border-2 border-dashed px-8 py-14 transition-colors ${
              dragging ? "border-brand-500 bg-brand-50" : "border-ink-200 bg-night-900/70 backdrop-blur"
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
                  className="mt-4 w-full rounded-lg border border-ink-200 bg-night-800 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-brand-500"
                  placeholder="Senha"
                />
                <div className="mt-4 flex gap-2">
                  <button
                    type="submit"
                    disabled={busy || !password}
                    className="rounded-lg bg-brand-500 px-4 py-2 font-medium text-night-950 hover:bg-brand-600 disabled:opacity-50"
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
                  className="inline-flex items-center gap-2 rounded-xl bg-brand-500 px-8 py-4 text-lg font-semibold text-night-950 shadow-glow hover:bg-brand-600 disabled:opacity-60"
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
            <div role="alert" className="mt-4 rounded-lg bg-red-500/10 px-4 py-3 text-red-300">
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

function NightScene() {
  return (
    <div className="night-scene" aria-hidden>
      <div className="beam b1" />
      <div className="beam b2" />
      <div className="rain" />
      <div className="fog" />
      <svg className="absolute inset-x-0 bottom-0 h-[34vh] w-full" viewBox="0 0 1440 360" preserveAspectRatio="xMidYMax slice">
        <defs>
          <linearGradient id="sky-far" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#151b27" />
            <stop offset="1" stopColor="#0b0e15" />
          </linearGradient>
        </defs>
        {/* fileira distante */}
        <path fill="url(#sky-far)" d="M0 360V210h40v-40h30v-30h20v30h30v60h50v-90h18l12-40 12 40h18v90h40v-50h60v-70h14V90l10-30 10 30v30h14v80h50v-40h40v60h30V150h60v-30h8V70l14-40 14 40v50h8v30h60v100h40v-60h50v-90h20l20-50 20 50h20v90h40v50h60V130h30v-20h50v20h30v130h40v-80h60v-60l18-46 18 46v60h40v120h40v-40h40V360z" />
        {/* fileira próxima, com janelas acesas */}
        <path fill="#06070a" d="M0 360V270h60v-50h50v30h40v-80h24l16-40 16 40h24v80h30v40h70v-110h20v-30l30-50 30 50v30h20v110h40v-60h60v-40h40v100h60v-130h16l24-60 24 60h16v130h40v-30h50v-70h70v100h40v-120h30l22-44 22 44h30v120h50v-40h60v60h40v-90h60v-30h20v30h60V360z" />
        <g fill="#f2b632" opacity=".55">
          <rect x="132" y="240" width="4" height="6" /><rect x="152" y="262" width="4" height="6" /><rect x="372" y="200" width="4" height="6" />
          <rect x="392" y="232" width="4" height="6" /><rect x="380" y="276" width="4" height="6" /><rect x="616" y="214" width="4" height="6" />
          <rect x="652" y="250" width="4" height="6" /><rect x="636" y="290" width="4" height="6" /><rect x="944" y="226" width="4" height="6" />
          <rect x="972" y="262" width="4" height="6" /><rect x="1190" y="290" width="4" height="6" /><rect x="1290" y="300" width="4" height="6" />
        </g>
      </svg>
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
