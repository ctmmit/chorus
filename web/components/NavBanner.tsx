import Link from "next/link";

import { MOCK_MODE } from "@/lib/config";

/** Edge-to-edge navy banner (Fulcrum masthead pattern) — small, present on
 * every page, not a full section divider. */
export function NavBanner() {
  return (
    <header className="flex items-baseline justify-between bg-navy px-5 py-3 text-ivory sm:px-8">
      <Link href="/" className="font-sans text-sm font-semibold tracking-[0.2em] uppercase">
        Chorus
      </Link>
      <nav className="flex items-center gap-5 font-sans text-[11px] tracking-[0.14em] uppercase">
        <Link href="/" className="text-ivory/80 hover:text-ivory">
          New digest
        </Link>
        <Link href="/compare" className="text-ivory/80 hover:text-ivory">
          Compare
        </Link>
        {MOCK_MODE ? <span className="text-blue">Mock mode</span> : null}
      </nav>
    </header>
  );
}
