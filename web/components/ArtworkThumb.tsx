"use client";

import { useState } from "react";

import { initialsFor } from "@/lib/subscribe";

/** Square show artwork, or the show's initials when there is none (or the
 * image fails to load). Decorative: the title always sits next to it. */
export function ArtworkThumb({
  url,
  title,
  size = 40,
}: {
  url: string | null;
  title: string;
  size?: number;
}) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const style = { width: size, height: size };

  if (!url || failedUrl === url) {
    return (
      <span
        aria-hidden="true"
        style={style}
        className="flex shrink-0 items-center justify-center border border-taupe font-serif text-xs text-silver"
      >
        {initialsFor(title)}
      </span>
    );
  }

  return (
    // Podcast artwork comes from arbitrary third-party hosts, so next/image
    // (which needs an allowlist per host) does not apply here.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={url}
      alt=""
      width={size}
      height={size}
      loading="lazy"
      referrerPolicy="no-referrer"
      style={style}
      className="shrink-0 border border-taupe object-cover"
      onError={() => setFailedUrl(url)}
    />
  );
}
