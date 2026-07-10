import { forwardRef } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

/** Labelled wrapper so form controls read like the Streamlit widgets. */
export function Field({
  label,
  htmlFor,
  hint,
  className,
  children,
}: {
  label: string;
  htmlFor?: string;
  hint?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <label htmlFor={htmlFor} className="text-[.82rem] font-semibold text-[#4B5563]">
        {label}
      </label>
      {children}
      {hint && <span className="text-xs text-[#8A8580]">{hint}</span>}
    </div>
  );
}

export interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
  options: string[];
}

/** Native select styled to match the brand — no Radix dependency required. */
export const Select = forwardRef<HTMLSelectElement, SelectProps>(
  ({ className, options, ...props }, ref) => (
    <div className="relative">
      <select
        ref={ref}
        className={cn(
          "h-10 w-full appearance-none rounded-md border border-parchment-border bg-white px-3 pr-9 text-sm text-ink transition-shadow focus:border-gold focus:outline-none focus:ring-4 focus:ring-gold/25 disabled:cursor-not-allowed disabled:opacity-60",
          className,
        )}
        {...props}
      >
        {options.map((opt) => (
          <option key={opt} value={opt}>
            {opt === "" ? "—" : opt}
          </option>
        ))}
      </select>
      <ChevronDown className="pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-[#8A8580]" />
    </div>
  ),
);
Select.displayName = "Select";
