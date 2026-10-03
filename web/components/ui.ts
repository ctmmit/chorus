/** Shared Tailwind class strings for the subscribe flow's controls, so the
 * wizard, the picker, and /subscriptions look like one surface.
 *
 * Fulcrum rules applied here: navy is the default ink for actions, Blue
 * appears only on the one primary action per view and on keyboard focus,
 * labels are silver small-caps (`label-caps` in globals.css), and numbers and
 * dates are mono. No cards, no shadows, no decorative fills. */

/** Blue keyboard-focus ring (Blue = focus). */
export const FOCUS_RING_CLASS =
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue";

const FOCUS = FOCUS_RING_CLASS;

export const INPUT_CLASS = `w-full border border-taupe bg-surface px-3 py-2 font-sans text-sm text-ink placeholder:text-silver ${FOCUS}`;

export const TEXTAREA_CLASS = `w-full border border-taupe bg-surface px-3 py-2 font-mono text-xs leading-relaxed text-ink placeholder:text-silver ${FOCUS}`;

export const NUMBER_INPUT_CLASS = `w-24 border border-taupe bg-surface px-3 py-2 font-mono text-sm text-ink ${FOCUS}`;

/** The one primary action in a view. */
export const PRIMARY_BUTTON_CLASS = `bg-blue px-5 py-2.5 font-sans text-xs uppercase tracking-wide text-ivory disabled:cursor-not-allowed disabled:opacity-40 ${FOCUS}`;

export const SECONDARY_BUTTON_CLASS = `border border-navy-text px-3 py-1.5 font-sans text-xs uppercase tracking-wide text-navy-text hover:bg-navy-text hover:text-ivory disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-navy-text ${FOCUS}`;

export const TEXT_BUTTON_CLASS = `font-sans text-xs text-navy-text underline underline-offset-2 hover:text-blue disabled:cursor-not-allowed disabled:opacity-40 ${FOCUS}`;

export const LINK_CLASS = `text-navy-text underline underline-offset-2 hover:text-blue ${FOCUS}`;

/** Inline error text (a genuinely negative signal, so Red). */
export const ERROR_TEXT_CLASS = "font-mono text-xs text-red";

export const HELP_TEXT_CLASS = "font-sans text-xs text-silver";

export const FIELD_LABEL_CLASS = "label-caps mb-1 block";
