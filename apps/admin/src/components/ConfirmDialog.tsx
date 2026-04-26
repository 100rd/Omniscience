interface Props {
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({ message, onConfirm, onCancel }: Props) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-overlay">
      <div className="bg-elevation-1 border border-border rounded-xl shadow-xl w-full max-w-sm px-6 py-6">
        <p className="text-sm text-text mb-6">{message}</p>
        <div className="flex justify-end gap-3">
          <button
            onClick={onCancel}
            className="px-4 py-2 text-sm rounded-lg border border-border text-text-secondary hover:bg-elevation-2 hover:text-text transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className="px-4 py-2 text-sm rounded-lg bg-danger-strong text-text-inverse hover:bg-danger-strong-hover transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Confirm
          </button>
        </div>
      </div>
    </div>
  );
}
