/** Poll only while a window is visible, with at most one request in flight.
 * Background jobs run in Python; hiding their UI must not cancel those jobs.
 */
export function visiblePolling(action: () => Promise<unknown>, delay: number, target: Document = document) {
  let disposed = false;
  let running = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const poll = async () => {
    clearTimeout(timer);
    if (disposed || running || target.hidden) return;
    running = true;
    try { await action(); }
    catch { /* Each caller owns user-facing errors; keep future polls alive. */ }
    finally {
      running = false;
      if (!disposed && !target.hidden) timer = setTimeout(() => void poll(), delay);
    }
  };
  const changed = () => { clearTimeout(timer); if (!target.hidden) void poll(); };
  target.addEventListener('visibilitychange', changed);
  void poll();
  return () => { disposed = true; clearTimeout(timer); target.removeEventListener('visibilitychange', changed); };
}
