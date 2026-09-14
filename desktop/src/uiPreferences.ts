export interface UiPreferences { theme: string; favorites: string[]; ui_state_synced: true }
const themes = new Set(['light', 'dark', 'system']);
export function validTheme(value: unknown): value is string { return typeof value === 'string' && themes.has(value); }
export function validFavorites(value: unknown): value is string[] { return Array.isArray(value) && value.every(item => typeof item === 'string'); }
export function migratedUiPreferences(preferences: Record<string, unknown>, localTheme: string | null, localFavorites: string | null): UiPreferences {
  let favorites: unknown;
  try { favorites = localFavorites === null ? undefined : JSON.parse(localFavorites); } catch { favorites = undefined; }
  return {
    theme: validTheme(localTheme) ? localTheme : validTheme(preferences.theme) ? preferences.theme : 'system',
    favorites: validFavorites(favorites) ? favorites : validFavorites(preferences.favorites) ? preferences.favorites : ['media', 'live'],
    ui_state_synced: true,
  };
}
