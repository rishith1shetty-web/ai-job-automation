export function WelcomeCard() {
  return (
    <section className="relative flex items-center gap-6 overflow-hidden rounded-[20px] border border-[#F3E4C8] bg-gradient-to-br from-white via-[#FEF7EA] to-[#FDEED6] px-8 py-6 shadow-card">
      <span
        aria-hidden
        className="pointer-events-none absolute -right-[6%] -top-[55%] size-[360px] rounded-full"
        style={{ background: "radial-gradient(circle, rgba(240,168,36,.16) 0%, rgba(240,168,36,0) 68%)" }}
      />
      <div className="relative">
        <span className="mb-2 inline-block rounded-full border border-[#F3D9A4] bg-white px-3 py-1 text-xs font-bold uppercase tracking-[.09em] text-gold-text">
          AI Recruitment Automation Platform
        </span>
        <h1 className="mb-1.5 text-2xl font-extrabold tracking-tight text-ink">
          Welcome to Rising Path Recruiters
        </h1>
        <p className="max-w-[60ch] text-[15px] leading-relaxed text-[#57534E]">
          Automate resume analysis, job matching, LinkedIn Easy Apply, Dice Easy Apply and
          application tracking from one intelligent dashboard.
        </p>
      </div>
    </section>
  );
}
