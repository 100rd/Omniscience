interface Props {
  value: string;
}

/*
 * Status -> semantic-token mapping.
 *   active / ok      -> success
 *   paused / partial -> warning
 *   error            -> danger
 *   running          -> info
 *   (anything else)  -> neutral (elevation-2 + text-secondary)
 */
const COLOR: Record<string, string> = {
  active: "bg-success-bg text-success-fg",
  ok: "bg-success-bg text-success-fg",
  paused: "bg-warning-bg text-warning-fg",
  partial: "bg-warning-bg text-warning-fg",
  error: "bg-danger-bg text-danger-fg",
  running: "bg-info-bg text-info-fg",
};

export function StatusBadge({ value }: Props) {
  const cls = COLOR[value] ?? "bg-elevation-2 text-text-secondary";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${cls}`}
    >
      {value}
    </span>
  );
}
