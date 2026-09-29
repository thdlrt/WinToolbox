export function moveSidebarItem(items: string[], source: string, target: string, after = false): string[] {
  if (source === target || !items.includes(source) || !items.includes(target)) return items;
  const next = items.filter(id => id !== source);
  next.splice(next.indexOf(target) + (after ? 1 : 0), 0, source);
  return next;
}
