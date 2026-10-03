"use client";

import { useId, useState } from "react";

import { clampInt } from "@/lib/subscribe";

import { FIELD_LABEL_CLASS, HELP_TEXT_CLASS, NUMBER_INPUT_CLASS } from "./ui";

/** A whole-number field that lets the person type freely (including a
 * temporarily empty or out-of-range value) but only ever reports a valid
 * integer within [min, max]; blur snaps the text to the nearest valid value. */
export function NumberField({
  label,
  hint,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  hint?: string;
  value: number;
  min: number;
  max: number;
  onChange: (next: number) => void;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const [text, setText] = useState(String(value));

  function handleChange(raw: string) {
    setText(raw);
    const n = Number(raw);
    if (raw.trim() !== "" && Number.isInteger(n) && n >= min && n <= max) onChange(n);
  }

  function handleBlur() {
    const snapped = clampInt(Number(text) || min, min, max);
    setText(String(snapped));
    onChange(snapped);
  }

  return (
    <div>
      <label htmlFor={id} className={FIELD_LABEL_CLASS}>
        {label}
      </label>
      <input
        id={id}
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        step={1}
        value={text}
        onChange={(e) => handleChange(e.target.value)}
        onBlur={handleBlur}
        aria-describedby={hintId}
        className={NUMBER_INPUT_CLASS}
      />
      <p id={hintId} className={`${HELP_TEXT_CLASS} mt-1`}>
        {hint ?? `${min} to ${max}`}
      </p>
    </div>
  );
}
