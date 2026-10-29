'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';

import { useEffect, useRef, useState } from 'react';
import type { InputHTMLAttributes, KeyboardEvent, ReactNode } from 'react';
import { Icon } from './icon';
import type { ConfigValue } from '@/lib/types';
import type { FieldOption } from '@/lib/config';
import { formatDuration } from '@/lib/config-layout';
import { MAX_CONNECTIVITY_TARGETS, connectivityTargetErrors, splitTargets } from '@/lib/connectivity';
import { MAX_ROUTE_NETWORKS, routeNetworkErrors } from '@/lib/routes';

/** Label, short help and origin on the left; the control and its message on the right. */
export function SettingRow({ id, label, labelFor, hint, badge, children }: {
  id: string; label: ReactNode; labelFor?: string; hint?: string; badge?: ReactNode; children: ReactNode;
}) {
  return <div className="setting-row" data-row={id}>
    <div className="setting-label">
      {labelFor ? <label id={id + '-label'} htmlFor={labelFor}>{label}</label> : <span className="setting-title" id={id + '-label'}>{label}</span>}
      {hint && <p className="setting-hint" id={id + '-hint'}>{hint}</p>}
      {badge}
    </div>
    <div className="setting-control">{children}</div>
  </div>;
}

/** Error count beside a tab or link: the number is visual, the sentence is read by screen readers. */
export function ErrorCount({ count }: { count: number }) {
  usePreferences();
  return <><span className="tab-error-count" aria-hidden="true">{count}</span><span className="sr-only">{t('{count} errors', { count })}</span></>;
}

/** Amber dot for unsaved changes, explained on hover and to screen readers. */
export function PendingDot({ className = 'nav-dirty' }: { className?: string }) {
  usePreferences();
  return <><span className={className} title={t("Unsaved changes")} aria-hidden="true" /><span className="sr-only">{t("Unsaved changes")}</span></>;
}

export function FieldError({ id, message }: { id: string; message?: string | null }) {
  usePreferences();
  return <p id={id} className="field-message field-message-error" aria-live="polite">
    {message ? <><Icon name="alert-circle" size={15} /><span>{t(message)}</span></> : null}</p>;
}

/** "Modified · was X" while a row differs from the saved configuration. */
export function ModifiedBadge({ previous }: { previous?: string | null }) {
  usePreferences();
  return <span className="modified-badge"><span className="modified-dot" aria-hidden="true" />
    {previous === null ? t("Will be changed") : previous === undefined ? t("Modified") : t("Modified · was {value}", { value: previous })}</span>;
}

export function Switch({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} type="checkbox" role="switch" className={'switch' + (className ? ' ' + className : '')} />;
}

/** Two to four short choices; native radios keep arrow keys and form semantics. */
export function Segmented({ id, value, options, onChange, disabled, labelledBy, describedBy, invalid, setting }: {
  id: string; value: ConfigValue | undefined; options: readonly FieldOption[]; onChange: (value: string) => void;
  disabled?: boolean; labelledBy?: string; describedBy?: string; invalid?: boolean; setting?: string;
}) {
  usePreferences();
  return <fieldset id={id} className="segmented" role="radiogroup" disabled={disabled} aria-labelledby={labelledBy}
    aria-describedby={describedBy} aria-invalid={invalid || undefined} data-setting={setting}>
    {options.map(option => <label key={option.value} className="segmented-option">
      <input type="radio" id={id + '-' + option.value} name={id} value={option.value} checked={value === option.value}
        onChange={() => onChange(option.value)} /><span>{t(option.label)}</span></label>)}
  </fieldset>;
}

export type CardOption = { value: string; label: string; description?: ReactNode };
/** Choices that need a sentence of explanation. Labels arrive translated. */
export function RadioCards({ id, value, options, onChange, disabled, labelledBy, describedBy, setting, grid = false }: {
  id: string; value: string; options: CardOption[]; onChange: (value: string) => void; disabled?: boolean;
  labelledBy?: string; describedBy?: string; setting?: string; grid?: boolean;
}) {
  return <fieldset id={id} className={'radio-cards' + (grid ? ' radio-cards-grid' : '')} role="radiogroup" disabled={disabled}
    aria-labelledby={labelledBy} aria-describedby={describedBy} data-setting={setting}>
    {options.map(option => <label key={option.value} className="radio-card">
      <input type="radio" id={id + '-' + option.value} name={id} value={option.value} checked={value === option.value}
        onChange={() => onChange(option.value)} />
      <span className="radio-card-text"><strong>{option.label}</strong>{option.description && <span>{option.description}</span>}</span>
    </label>)}
  </fieldset>;
}

export function NumericInput({ value, onValueChange, ...props }: Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange'> & {
  value: ConfigValue;
  onValueChange: (value: number) => void;
}) {
  usePreferences();
  const display = (current: ConfigValue) => typeof current === 'number' && !Number.isFinite(current) ? '' : String(current);
  const [raw, setRaw] = useState(() => display(value));
  useEffect(() => {
    setRaw(previous => {
      const parsed = previous === '' ? NaN : Number(previous);
      return Object.is(parsed, value) ? previous : display(value);
    });
  }, [value]);
  return <input {...props} type="number" value={raw} onChange={event => {
    const next = event.currentTarget.value;
    setRaw(next);
    onValueChange(next === '' ? NaN : Number(next));
  }} />;
}

/** The unit sits inside the field; long durations also read as minutes or hours. */
export function NumberField({ unit, duration, className, ...props }: Parameters<typeof NumericInput>[0] & { unit?: string; duration?: boolean }) {
  usePreferences();
  const caption = duration ? formatDuration(props.value) : null;
  return <>
    <div className={'input-group' + (className ? ' ' + className : '')}>
      <NumericInput {...props} className="mono" inputMode={props.step === 1 ? 'numeric' : 'decimal'} />
      {unit && <span className="input-unit" aria-hidden="true">{t(unit)}</span>}
    </div>
    {caption && <p className="field-caption">= {caption}</p>}
  </>;
}

/** Small integer ranges: the buttons stay out of the tab order around a real input. */
export function Stepper({ id, value, min, max, onValueChange, disabled, ...props }: Parameters<typeof NumericInput>[0] & { min: number; max: number }) {
  usePreferences();
  const number = typeof value === 'number' && Number.isFinite(value) ? value : null;
  const change = (delta: number) => onValueChange(Math.min(max, Math.max(min, (number ?? min) + delta)));
  return <div className="stepper">
    <button type="button" tabIndex={-1} aria-label={t("Decrease")} aria-controls={id} disabled={disabled || (number !== null && number <= min)}
      onClick={() => change(-1)}><Icon name="minus" size={16} /></button>
    <NumericInput {...props} id={id} value={value} min={min} max={max} step={1} disabled={disabled} inputMode="numeric" className="mono" onValueChange={onValueChange} />
    <button type="button" tabIndex={-1} aria-label={t("Increase")} aria-controls={id} disabled={disabled || (number !== null && number >= max)}
      onClick={() => change(1)}><Icon name="plus" size={16} /></button>
  </div>;
}

const chipKinds = {
  targets: { max: MAX_CONNECTIVITY_TARGETS, errors: connectivityTargetErrors, placeholder: "IPv4:port, then Enter" },
  networks: { max: MAX_ROUTE_NETWORKS, errors: routeNetworkErrors, placeholder: "auto or network/prefix, then Enter" },
};

/** Connectivity targets or routed networks as chips; the stored value stays the comma-separated list the backend expects. */
export function ChipList({ id, name, value, onChange, disabled, invalid, describedBy, setting, fieldError, errorId, kind = 'targets' }: {
  id: string; name?: string; value: ConfigValue; onChange: (value: string) => void; disabled?: boolean;
  invalid?: boolean; describedBy?: string; setting?: string; fieldError?: string; errorId: string; kind?: keyof typeof chipKinds;
}) {
  usePreferences();
  const rules = chipKinds[kind];
  const items = splitTargets(value), problems = rules.errors(items);
  const [text, setText] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const full = items.length >= rules.max;
  const commit = (raw: string) => {
    const added = raw.split(/[\s,;]+/).map(part => part.trim()).filter(Boolean);
    if (added.length) onChange([...items, ...added].join(','));
    setText('');
  };
  const remove = (index: number) => onChange(items.filter((_, position) => position !== index).join(','));
  const keyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter' || event.key === ',') { event.preventDefault(); commit(text); }
    else if (event.key === 'Backspace' && !text && items.length) { event.preventDefault(); remove(items.length - 1); }
  };
  const listed = problems.map((problem, index) => problem ? { target: items[index], problem } : null).filter(Boolean) as { target: string; problem: string }[];
  return <>
    <div className={'chip-input' + (disabled ? ' is-disabled' : '') + (invalid ? ' is-invalid' : '')} data-setting={setting}
      onClick={event => { if (event.target === event.currentTarget) input.current?.focus(); }}>
      {items.map((item, index) => <span key={item + ':' + index} className={'chip' + (problems[index] ? ' chip-invalid' : '')}>
        <span className="mono">{item}</span>{problems[index] && <span className="sr-only"> ({t(problems[index]!)})</span>}
        <button type="button" aria-label={t("Remove {item}", { item })} disabled={disabled} onClick={() => remove(index)}><Icon name="close" size={13} /></button>
      </span>)}
      <input ref={input} id={id} name={name} value={text} disabled={disabled} placeholder={full ? '' : t(rules.placeholder)}
        autoComplete="off" spellCheck={false} aria-invalid={invalid || undefined} aria-describedby={describedBy}
        onChange={event => { const next = event.currentTarget.value; if (/[,;\n]/.test(next)) commit(next); else setText(next); }}
        onKeyDown={keyDown} onBlur={() => { if (text.trim()) commit(text); }} />
    </div>
    <p className="field-caption">{t("{count} of {max}", { count: items.length, max: rules.max })}</p>
    {/* A rejected chip explains itself; list-level rules (count, empty list) use the field error. */}
    <p id={errorId} className="field-message field-message-error" aria-live="polite">
      {listed.length > 0 ? <><Icon name="alert-circle" size={15} /><span>{listed.map(item => item.target + ': ' + t(item.problem)).join(' · ')}</span></>
        : fieldError ? <><Icon name="alert-circle" size={15} /><span>{t(fieldError)}</span></> : null}</p>
  </>;
}

/** One field with Show/Hide instead of typing the password twice. */
export function PasswordInput({ id, name, value, onChange, disabled, invalid, describedBy }: {
  id: string; name?: string; value: string; onChange: (value: string) => void; disabled?: boolean; invalid?: boolean; describedBy?: string;
}) {
  usePreferences();
  const [shown, setShown] = useState(false);
  const long = Array.from(value).length >= 8;
  return <>
    <div className="input-group">
      <input id={id} name={name} type={shown ? 'text' : 'password'} autoComplete="new-password" value={value} disabled={disabled}
        placeholder={t("Keep current password")} aria-invalid={invalid || undefined} aria-describedby={describedBy}
        spellCheck={false} onChange={event => onChange(event.currentTarget.value)} />
      <button type="button" id={id + '-toggle'} className="input-action" aria-pressed={shown} aria-controls={id} disabled={disabled}
        onClick={() => setShown(current => !current)}>{shown ? t("Hide") : t("Show")}</button>
    </div>
    {value && <p className={'password-rule' + (long ? ' is-met' : ' is-unmet')}><Icon name={long ? 'check' : 'circle'} size={14} />{t("8 characters or more")}</p>}
  </>;
}
