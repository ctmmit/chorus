import Link from "next/link";

import { MOCK_MODE } from "@/lib/config";

/** Edge-to-edge navy banner (Fulcrum masthead pattern) — small, present on
 * every page, not a full section divider. */
export function NavBanner() {
  return (
    <header className="flex flex-wrap items-baseline justify-between gap-x-5 gap-y-2 bg-navy px-5 py-3 text-white sm:px-8">
      <Link href="/" className="font-sans text-sm font-semibold tracking-[0.2em] uppercase">
        Chorus
      </Link>
      <nav className="flex flex-wrap items-center gap-x-5 gap-y-1 font-sans text-[11px] tracking-[0.14em] uppercase">
        <Link href="/subscribe" className="text-white/80 hover:text-white">
          Subscribe
        </Link>
        <Link href="/subscriptions" className="text-white/80 hover:text-white">
          Subscriptions
        </Link>
        <Link href="/" className="text-white/80 hover:text-white">
          New digest
        </Link>
        <Link href="/compare" className="text-white/80 hover:text-white">
          Compare
        </Link>
        <Link href="/network" className="text-white/80 hover:text-white">
          Network
        </Link>
        {MOCK_MODE ? <span className="text-blue">Mock mode</span> : null}
      </nav>
    </header>
  );
}
