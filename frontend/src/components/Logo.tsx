export function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-2.5 select-none">
      <svg width={compact ? 26 : 32} height={compact ? 26 : 32} viewBox="0 0 32 32" aria-hidden>
        <rect width="32" height="32" rx="8" fill="#2f6bff" />
        <path d="M10 7h8.5L24 12.5V24a1 1 0 0 1-1 1H10a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1z" fill="#fff" />
        <path d="M18.5 7v5.5H24" fill="#dbe7ff" />
        <path d="M12.5 21.5l1-3.2 5.6-5.6 2.2 2.2-5.6 5.6z" fill="#2f6bff" />
      </svg>
      <span className={`whitespace-nowrap font-bold tracking-tight text-ink-900 ${compact ? "text-sm" : "text-lg"}`}>
        UNIVERSAL <span className="text-brand-500">PDF</span> EDITOR
      </span>
    </div>
  );
}
