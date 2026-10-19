export function Logo({ compact = false }: { compact?: boolean }) {
  const s = compact ? 28 : 36;
  return (
    <div className="flex items-center gap-2.5 select-none">
      <svg width={s} height={s} viewBox="0 0 32 32" aria-hidden>
        <defs>
          <linearGradient id="upe-gold" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#ffd979" />
            <stop offset="1" stopColor="#e09a12" />
          </linearGradient>
        </defs>
        {/* janela em arco gótico com uma folha de documento */}
        <path d="M3 30V13C3 6.5 9 2 16 2s13 4.5 13 11v17z" fill="#0d1017" stroke="url(#upe-gold)" strokeWidth="1.6" />
        <path d="M11 9h7l4 4v11.2a.8.8 0 0 1-.8.8H11a.8.8 0 0 1-.8-.8V9.8A.8.8 0 0 1 11 9z" fill="url(#upe-gold)" />
        <path d="M18 9v4h4" fill="#7a5408" />
        <path d="M12.6 21.8l.8-2.6 4.6-4.6 1.8 1.8-4.6 4.6z" fill="#0d1017" />
      </svg>
      <span className={`whitespace-nowrap font-display font-bold tracking-[0.14em] text-ink-900 ${compact ? "text-sm" : "text-lg"}`}>
        UNIVERSAL <span className="text-brand-500 drop-shadow-[0_0_8px_rgba(242,182,50,.55)]">PDF</span> EDITOR
      </span>
    </div>
  );
}
