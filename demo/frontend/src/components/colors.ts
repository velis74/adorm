/** A Vuetify colour per session or flow name, the same in the timeline and the SQL list. */
const COLORS: Record<string, string> = {
  A: 'blue',
  A2: 'light-blue',
  B: 'deep-orange',
  B2: 'orange',
  C: 'purple',
  D: 'teal',
  demo: 'grey',
  example: 'primary',
};

export function sessionColor(name: string | null): string {
  return name === null ? 'grey-lighten-1' : (COLORS[name] ?? 'secondary');
}
