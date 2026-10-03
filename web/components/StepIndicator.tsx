"use client";

import { WIZARD_STEPS, type WizardStep } from "@/lib/subscribe";

import { FOCUS_RING_CLASS } from "./ui";

/** The wizard's step list. Steps up to `furthest` are buttons (keyboard
 * operable, Tab then Enter); later steps are plain text until reachable.
 * The current step carries aria-current="step". */
export function StepIndicator({
  current,
  furthest,
  onSelect,
}: {
  current: WizardStep;
  furthest: WizardStep;
  onSelect: (step: WizardStep) => void;
}) {
  return (
    <nav aria-label="Subscribe progress">
      <ol className="flex flex-wrap gap-x-8 gap-y-2">
        {WIZARD_STEPS.map((s) => {
          const isCurrent = s.id === current;
          const reachable = s.id <= furthest;
          const content = (
            <>
              <span className="mr-2 font-mono text-xs">{String(s.id).padStart(2, "0")}</span>
              <span className="font-sans text-xs uppercase tracking-[0.12em]">{s.label}</span>
            </>
          );
          return (
            <li key={s.id}>
              {reachable ? (
                <button
                  type="button"
                  onClick={() => onSelect(s.id)}
                  aria-current={isCurrent ? "step" : undefined}
                  className={`${FOCUS_RING_CLASS} ${
                    isCurrent ? "font-bold text-navy-text" : "text-navy-text/80 hover:text-navy-text"
                  }`}
                >
                  {content}
                </button>
              ) : (
                <span className="text-silver" aria-disabled="true">
                  {content}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
