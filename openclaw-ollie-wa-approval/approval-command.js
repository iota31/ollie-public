const COMMAND_RE = /^(approve|deny)(?:\s+((?:H|W)-[A-Za-z0-9_-]{3,32}))?\s*$/i;

/** Parse the deliberately small, channel-neutral owner approval language.
 *  Refs are treated as opaque case-sensitive strings.
 *  Only uppercase H-/W- prefixes are valid at parse time.
 *  The captured ref bytes after the uppercase prefix are preserved exactly.
 */
export function parseApprovalCommand(text) {
  if (typeof text !== "string") return null;
  const match = text.trim().match(COMMAND_RE);
  if (!match) return null;
  const rawRef = match[2];
  if (rawRef) {
    // Reject lowercase or mixed-case prefix at parse time.
    // Only uppercase H- or W- is authoritative.
    if (!/^([HW])-/i.test(rawRef)) {
      return null;
    }
    const prefix = rawRef[0];
    if (prefix !== "H" && prefix !== "W") {
      return null;
    }
  }
  // Preserve the exact captured ref bytes; do not upper-case.
  const ref = rawRef ? rawRef : null;
  return {
    decision: match[1].toLowerCase(),
    ref,
  };
}

/** Select the only request, or require a typed short reference when ambiguous. */
export function selectPending(command, pending) {
  if (command.ref) return { ref: command.ref };
  if (pending.length === 1) return { ref: pending[0].ref };
  if (pending.length === 0) return { error: "There is no pending approval." };
  return {
    error: `There are ${pending.length} pending approvals. Reply ${command.decision} H-XXXX or W-XXXX.`,
  };
}
