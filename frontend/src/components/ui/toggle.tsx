import { cn } from "@/lib/utils";

/** A controlled switch matching Streamlit's st.toggle. */
export function Toggle({
  checked,
  onCheckedChange,
  label,
  id,
  disabled,
}: {
  checked: boolean;
  onCheckedChange: (next: boolean) => void;
  label?: string;
  id?: string;
  disabled?: boolean;
}) {
  return (
    <label htmlFor={id} className="inline-flex cursor-pointer select-none items-center gap-2.5">
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onCheckedChange(!checked)}
        className={cn(
          "relative h-6 w-11 shrink-0 rounded-full border transition-colors duration-200 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-gold/40 disabled:opacity-50",
          checked ? "border-gold-deep bg-gradient-to-r from-gold to-gold-deep" : "border-parchment-border bg-[#E7E0D8]",
        )}
      >
        <span
          className={cn(
            "absolute top-0.5 size-5 rounded-full bg-white shadow transition-transform duration-200",
            checked ? "translate-x-[22px]" : "translate-x-0.5",
          )}
        />
      </button>
      {label && <span className="text-sm font-semibold text-[#4B5563]">{label}</span>}
    </label>
  );
}

/** A checkbox matching Streamlit's st.checkbox. */
export function Checkbox({
  checked,
  onCheckedChange,
  label,
  id,
}: {
  checked: boolean;
  onCheckedChange: (next: boolean) => void;
  label: string;
  id?: string;
}) {
  return (
    <label htmlFor={id} className="inline-flex cursor-pointer select-none items-center gap-2 text-sm text-[#4B5563]">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={(e) => onCheckedChange(e.target.checked)}
        className="size-4 rounded border-parchment-border text-gold accent-[#D97706] focus:ring-gold"
      />
      {label}
    </label>
  );
}
