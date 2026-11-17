/** Per-browser settings of the demo in localStorage; reading or writing may fail (private windows, blocked storage). */

export function loadPreference(key: string): string | null {
  try {
    return window.localStorage.getItem(`adorm-demo.${key}`);
  } catch {
    return null;
  }
}

export function savePreference(key: string, value: string): void {
  try {
    window.localStorage.setItem(`adorm-demo.${key}`, value);
  } catch {
    // the setting lasts until the page is reloaded
  }
}

/** The stored theme, else the system's colour scheme. */
export function initialTheme(): 'light' | 'dark' {
  const stored = loadPreference('theme');
  if (stored === 'light' || stored === 'dark') return stored;
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
