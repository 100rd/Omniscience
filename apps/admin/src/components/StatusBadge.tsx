interface Props {
  value: string;
}

const COLOR: Record<string, string> = {
  active: "bg-green-100 text-green-800",
  ok: "bg-green-100 text-green-800",
  paused: "bg-yellow-100 text-yellow-800",
  partial: "bg-yellow-100 text-yellow-800",
  error: "bg-red-100 text-red-800",
  running: "bg-blue-100 text-blue-800",
};

export function StatusBadge({ value }: Props) {
  const cls = COLOR[value] ?? "bg-gray-100 text-gray-700";
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${cls}`}>
      {value}
    </span>
  );
}
