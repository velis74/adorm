import hljs from 'highlight.js/lib/core';
import python from 'highlight.js/lib/languages/python';
import sql from 'highlight.js/lib/languages/sql';

hljs.registerLanguage('python', python);
hljs.registerLanguage('sql', sql);

export type Language = 'python' | 'sql';

/** `code` as HTML with highlight.js token spans; the text is escaped. */
export function highlight(code: string, language: Language): string {
  return hljs.highlight(code, { language, ignoreIllegals: true }).value;
}
