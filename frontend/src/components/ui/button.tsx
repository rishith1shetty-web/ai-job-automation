import { forwardRef } from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-semibold transition-all duration-200 ease-brand focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-gold/40 disabled:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        // Gold gradient = Streamlit type="primary"
        primary:
          "border border-gold-deep bg-gradient-to-br from-[#F5A623] to-gold-deep text-white shadow-[0_2px_8px_rgba(217,119,6,.28)] hover:enabled:from-[#F6B03C] hover:enabled:to-[#C2680A] hover:enabled:shadow-[0_4px_14px_rgba(217,119,6,.38)] disabled:opacity-45",
        // White card = Streamlit default button
        default:
          "border border-parchment-border bg-white text-brown-deep shadow-[0_1px_2px_rgba(62,46,30,.06)] hover:enabled:-translate-y-px hover:enabled:border-gold hover:enabled:text-gold-text hover:enabled:shadow-lift disabled:opacity-45",
        destructive:
          "border border-red-200 bg-white text-destructive shadow-[0_1px_2px_rgba(62,46,30,.06)] hover:enabled:border-destructive hover:enabled:bg-red-50 disabled:opacity-45",
        ghost: "text-brown-deep hover:enabled:bg-parchment-amber disabled:opacity-45",
      },
      size: {
        default: "h-10 px-4 py-2",
        sm: "h-9 px-3",
        lg: "h-11 px-6",
        full: "h-10 w-full px-4",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, type = "button", ...props }, ref) => (
    <button ref={ref} type={type} className={cn(buttonVariants({ variant, size, className }))} {...props} />
  ),
);
Button.displayName = "Button";

export { buttonVariants };
