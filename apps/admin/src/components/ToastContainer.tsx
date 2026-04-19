import { Toast } from "../hooks/useToast";

interface Props {
  toasts: Toast[];
  onRemove: (id: number) => void;
}

export function ToastContainer({ toasts, onRemove }: Props) {
  if (toasts.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`flex items-start gap-3 rounded-lg px-4 py-3 shadow-lg text-sm text-white max-w-sm ${
            t.type === "error"
              ? "bg-red-600"
              : t.type === "success"
                ? "bg-green-600"
                : "bg-gray-800"
          }`}
        >
          <span className="flex-1">{t.message}</span>
          <button
            onClick={() => onRemove(t.id)}
            className="ml-2 opacity-70 hover:opacity-100 leading-none"
            aria-label="Dismiss"
          >
            &times;
          </button>
        </div>
      ))}
    </div>
  );
}
