/** Choose axis ticks without removing any plotted points. */
export function chartTicks(labels: readonly string[], count: number, bars = false) {
  const length = Math.max(0, Math.trunc(count));
  if (!length) return [];
  const ticks = Math.min(length, 5);
  return Array.from({ length: ticks }, (_, position) => {
    const index = ticks === 1 ? 0 : Math.round(position * (length - 1) / (ticks - 1));
    return {
      index,
      label: labels[index] || String(index + 1),
      percent: bars ? (index + 0.5) / length * 100 : length === 1 ? 50 : index / (length - 1) * 100,
    };
  });
}
