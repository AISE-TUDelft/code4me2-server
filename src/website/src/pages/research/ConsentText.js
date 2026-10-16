import React from "react";
import Icon from "../../components/common/Icon";

// Only web and mail links become anchors; anything else (javascript:, data:)
// stays literal text. Trailing punctuation is not part of a link.
const LINK_PATTERN = /(https?:\/\/[^\s<>"']+|mailto:[^\s<>"']+)/g;
const TRAILING_PUNCTUATION = /[.,;:!?)\]]+$/;

/** Splits plain text into strings and link elements (never parsed as HTML). */
export const linkify = (text) => {
  const source = typeof text === "string" ? text : "";
  const parts = [];
  let last = 0;
  for (const match of source.matchAll(LINK_PATTERN)) {
    const trailing = (match[0].match(TRAILING_PUNCTUATION) || [""])[0];
    const href = match[0].slice(0, match[0].length - trailing.length);
    if (match.index > last) parts.push(source.slice(last, match.index));
    parts.push(
      <a key={`${match.index}-${href}`} href={href} target="_blank" rel="noopener noreferrer">
        {href}
      </a>,
    );
    last = match.index + href.length;
  }
  if (last < source.length) parts.push(source.slice(last));
  return parts;
};

/**
 * A researcher's consent document or the platform notice, shown exactly as
 * typed: line breaks are kept and links are clickable.
 */
const ConsentText = ({ text, className = "" }) => (
  <div className={`consent-text${className ? ` ${className}` : ""}`}>{linkify(text)}</div>
);

/**
 * What a participant reviews before joining: the study's own document (if
 * any), the platform notice of what is recorded, and the statements to tick.
 * Without `onChange` nothing is a form control: the statements are listed,
 * with ticks from `answers` when given (the participant's own record) or as
 * required/optional otherwise (owner previews).
 */
export const ConsentReview = ({ consent, answers = null, onChange, disabled = false, idPrefix = "consent" }) => {
  if (!consent) return null;
  const statements = Array.isArray(consent.statements) ? consent.statements : [];
  const readOnly = typeof onChange !== "function";
  return (
    <div className="consent-review">
      {consent.document ? <ConsentText text={consent.document} className="consent-document" /> : null}
      {consent.notice ? (
        <div className="consent-notice" aria-label="Consent notice">
          {consent.document ? <h4 className="ui-section-title">What the platform records</h4> : null}
          <ConsentText text={consent.notice} />
        </div>
      ) : null}
      {statements.length && readOnly ? (
        <ul className="consent-answers" aria-label="Consent statements">
          {statements.map((statement) => {
            const ticked = answers ? answers[statement.id] === true : null;
            return (
              <li key={statement.id}>
                {ticked === null ? null : (
                  <Icon name={ticked ? "check" : "x"} size={14} title={ticked ? "Ticked" : "Not ticked"} />
                )}
                <span>
                  {statement.text}
                  <small>{statement.required ? " (required)" : " (optional)"}</small>
                </span>
              </li>
            );
          })}
        </ul>
      ) : null}
      {statements.length && !readOnly ? (
        <div className="consent-statements" role="group" aria-label="Consent statements">
          {statements.map((statement) => (
            <label key={statement.id} className="ui-check" htmlFor={`${idPrefix}-${statement.id}`}>
              <input
                id={`${idPrefix}-${statement.id}`}
                type="checkbox"
                checked={(answers || {})[statement.id] === true}
                onChange={(event) => onChange(statement.id, event.target.checked)}
                disabled={disabled}
              />
              <span className="ui-check-text">
                {statement.text}
                {statement.required ? null : <small> (optional)</small>}
              </span>
            </label>
          ))}
        </div>
      ) : null}
    </div>
  );
};

export default ConsentText;
