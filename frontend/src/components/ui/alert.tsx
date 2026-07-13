import { cva, type VariantProps } from "class-variance-authority";
import { CheckCircle2, Info, AlertTriangle, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";

const alertVariants = cva("flex items-start gap-2.5 rounded-xl border px-3.5 py-2.5 text-sm", {
  variants: {
    variant: {
      info: "border-parchment-border bg-white text-[#4B5563]",
      success: "border-green-200 bg-green-50 text-green-700",
      warn: "border-amber-200 bg-amber-50 text-amber-800",
      danger: "border-red-200 bg-red-50 text-destructive",
    },
  },
  defaultVariants: { variant: "info" },
});

const ICONS = { info: Info, success: CheckCircle2, warn: AlertTriangle, danger: XCircle };

export interface AlertProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof alertVariants> {}

export function Alert({ className, variant = "info", children, ...props }: AlertProps) {
  const Icon = ICONS[variant ?? "info"];
  // Errors/warnings announce assertively; info/success politely. Keeps screen-reader
  // feedback meaningful without changing any of the visible wording.
  const assertive = variant === "danger" || variant === "warn";
  return (
    <div
      className={cn(alertVariants({ variant }), className)}
      role={assertive ? "alert" : "status"}
      aria-live={assertive ? "assertive" : "polite"}
      {...props}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="font-semibold leading-snug">{children}</div>
    </div>
  );
}
