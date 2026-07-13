import { cn } from "@/lib/utils";

export function Table({ className, ...props }: React.TableHTMLAttributes<HTMLTableElement>) {
  return (
    // Scroll region for BOTH axes: wide tables scroll horizontally inside this box, and
    // tall tables scroll vertically (capped at 70vh) so the sticky header stays pinned
    // to the top of the table rather than scrolling away with the page.
    <div className="max-h-[70vh] w-full overflow-auto rounded-lg border border-parchment-border bg-card shadow-card">
      <table className={cn("w-full border-collapse text-sm", className)} {...props} />
    </div>
  );
}

export function TableHeader({ className, ...props }: React.HTMLAttributes<HTMLTableSectionElement>) {
  return <thead className={cn("", className)} {...props} />;
}

export function TableBody({ className, ...props }: React.HTMLAttributes<HTMLTableSectionElement>) {
  return <tbody className={cn("", className)} {...props} />;
}

export function TableRow({ className, ...props }: React.HTMLAttributes<HTMLTableRowElement>) {
  return (
    <tr
      className={cn(
        // Subtle zebra for scan-ability; hover is a stronger amber so it always reads on
        // top of the zebra tint.
        "border-b border-[#F1EBE2] transition-colors last:border-0 even:bg-[#FCFAF6] hover:bg-[#FDF0DC]",
        className,
      )}
      {...props}
    />
  );
}

export function TableHead({ className, ...props }: React.ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      className={cn(
        // Sticky within the page scroll so long tables keep their column labels visible.
        // The opaque header background prevents rows bleeding through when pinned.
        "sticky top-0 z-10 whitespace-nowrap border-b border-parchment-border bg-[#FBF7F0] px-3.5 py-2.5 text-left text-[.72rem] font-extrabold uppercase tracking-[.06em] text-brown-deep shadow-[0_1px_0_rgba(231,224,216,1)]",
        className,
      )}
      {...props}
    />
  );
}

export function TableCell({ className, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td className={cn("px-3.5 py-2.5 align-middle text-[#57534E]", className)} {...props} />
  );
}
