import { Toast } from "../hooks/useToast";

interface Props {
  toasts: Toast[];
  onRemove: (id: number) => void;
}

/*
 * Toast tone -> semantic-token mapping.
 *
 * Toasts use the SOLID status colour rather than the soft `*-bg` tint so
 * they remain visually loud against the page surface. We pair each tone
 * with `text-text-inverse` (white) which crosses the AA threshold against
 * every solid status color in both themes.
 */
const TONE: Record<Toast["type"], string> = {
  error: "bg-danger-strong text-text-inverse",
  success: "bg-success-fg text-text-inverse",
  info: "bg-elevation-2 text-text border border-border",
};

export function ToastContainer({ toasts, onRemove }: Props) {
  if (toasts.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`flex items-start gap-3 rounded-lg px-4 py-3 shadow-lg text-sm max-w-sm ${TONE[t.type]}`}
        >
          <span className="flex-1">{t.message}</span>
          <button
            onClick={() => onRemove(t.id)}
            className="ml-2 opacity-70 hover:opacity-100 leading-none focus:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
            aria-label="Dismiss"
          >
            &times;
          </button>
        </div>
      ))}
    </div>
  );
}
