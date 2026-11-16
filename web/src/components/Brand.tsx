export default function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`brand ${compact ? "compact" : ""}`}>
      <svg viewBox="0 0 32 32" aria-hidden="true">
        <rect width="32" height="32" rx="7" fill="#0f1b2d" />
        <path d="M6 21h20M8 21V11h10v10M18 14h5l3 4v3" fill="none" stroke="#f5b841" strokeWidth="2.4" strokeLinejoin="round" />
        <circle cx="11" cy="22.5" r="2" fill="#f5b841" />
        <circle cx="22" cy="22.5" r="2" fill="#f5b841" />
      </svg>
      <span>
        SetuHaul <em>Dock Desk</em>
      </span>
    </div>
  );
}
