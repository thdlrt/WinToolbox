export type SettingsDraft<T> = { saved: T; draft: T };

const equal = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

export function changedSettings<T extends object>(saved: T, draft: T): Partial<T> {
  return Object.fromEntries(Object.entries(draft).filter(([key, value]) =>
    !equal(saved[key as keyof T], value))) as Partial<T>;
}

// Status polling and printer import may update settings while the form has edits.
export function receiveSettings<T extends object>(current: SettingsDraft<T> | undefined, incoming: T): SettingsDraft<T> {
  const edits = current ? changedSettings(current.saved, current.draft) : {};
  return { saved: incoming, draft: { ...incoming, ...edits } };
}

// Keep edits made after a save started; acknowledge only that submitted snapshot.
export function savedSettings<T extends object>(current: SettingsDraft<T>, submitted: T, incoming: T): SettingsDraft<T> {
  return { saved: incoming, draft: { ...incoming, ...changedSettings(submitted, current.draft) } };
}

export function settingsPayload<T extends object>(current: SettingsDraft<T>, token: string, printerCode: string): Record<string, unknown> {
  const changes = changedSettings(current.saved, current.draft) as Record<string, unknown>;
  delete changes.has_token;
  delete changes.has_printer_access_code;
  return { ...changes, ...(token ? { token } : {}), ...(printerCode ? { printer_access_code: printerCode } : {}) };
}
