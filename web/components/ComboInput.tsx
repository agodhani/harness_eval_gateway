"use client";

/**
 * Free-text input with a suggestion dropdown (ARIA combobox pattern).
 *
 * Replaces `<input list>` + `<datalist>`: the native datalist popup is
 * unstyleable and inconsistently supported (browsers without datalist support
 * render the <option> elements in-flow as plain text). This renders a real
 * overlay anchored to the input — options are always readable and never
 * clipped by surrounding layout.
 */
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

export function ComboInput({
  id,
  value,
  onChange,
  suggestions,
  placeholder,
  disabled = false,
  mono = false,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  suggestions: string[];
  placeholder?: string;
  disabled?: boolean;
  /** Render the input in the mono font variant (.input--mono). */
  mono?: boolean;
}) {
  const [open, setOpen] = useState(false);
  // True while typing — narrows the list; false shows every suggestion.
  const [filtering, setFiltering] = useState(false);
  const [active, setActive] = useState(-1);
  const rootRef = useRef<HTMLDivElement>(null);
  const listboxId = useId();

  const items = useMemo(() => {
    if (!filtering) return suggestions;
    const q = value.trim().toLowerCase();
    return suggestions.filter((s) => !q || s.toLowerCase().includes(q));
  }, [filtering, suggestions, value]);

  const expanded = open && items.length > 0;

  // Close when clicking anywhere outside the control.
  useEffect(() => {
    if (!open) return;
    function onDocMouseDown(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, [open]);

  // Keep the keyboard-highlighted option scrolled into view.
  useEffect(() => {
    if (!expanded || active < 0) return;
    document
      .getElementById(`${listboxId}-opt-${active}`)
      ?.scrollIntoView({ block: "nearest" });
  }, [active, expanded, listboxId]);

  function openList(unfiltered: boolean) {
    setFiltering(!unfiltered);
    setActive(-1);
    setOpen(true);
  }

  function choose(v: string) {
    onChange(v);
    setOpen(false);
    setActive(-1);
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) openList(true);
      else setActive((a) => Math.min(a + 1, items.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (open) setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      // Never submit the surrounding form while the list is open.
      if (open) {
        e.preventDefault();
        if (active >= 0 && items[active] != null) choose(items[active]);
        else setOpen(false);
      }
    } else if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        setOpen(false);
      }
    }
  }

  return (
    <div className="combo" ref={rootRef}>
      <input
        id={id}
        className={mono ? "input input--mono" : "input"}
        role="combobox"
        aria-expanded={expanded}
        aria-controls={expanded ? listboxId : undefined}
        aria-activedescendant={
          expanded && active >= 0 ? `${listboxId}-opt-${active}` : undefined
        }
        aria-autocomplete="list"
        autoComplete="off"
        spellCheck={false}
        placeholder={placeholder}
        value={value}
        disabled={disabled}
        onChange={(e) => {
          onChange(e.target.value);
          openList(false);
        }}
        onFocus={() => openList(true)}
        onKeyDown={onKeyDown}
        onBlur={(e) => {
          // Option clicks use onMouseDown + preventDefault, so a blur here
          // means focus genuinely left the control.
          if (!rootRef.current?.contains(e.relatedTarget as Node)) {
            setOpen(false);
          }
        }}
      />
      <button
        type="button"
        className="combo__toggle"
        tabIndex={-1}
        aria-label={expanded ? "Hide suggestions" : "Show suggestions"}
        disabled={disabled}
        onMouseDown={(e) => e.preventDefault()} // keep focus inside the input
        onClick={() => (open ? setOpen(false) : openList(true))}
      >
        ▾
      </button>
      {expanded ? (
        <ul
          className="combo__list"
          role="listbox"
          id={listboxId}
          aria-label="Suggestions"
        >
          {items.map((s, i) => (
            <li
              key={s}
              id={`${listboxId}-opt-${i}`}
              role="option"
              aria-selected={i === active}
              className={
                i === active
                  ? "combo__option combo__option--active"
                  : "combo__option"
              }
              title={s}
              onMouseDown={(e) => {
                e.preventDefault();
                choose(s);
              }}
              onMouseEnter={() => setActive(i)}
            >
              {s}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
