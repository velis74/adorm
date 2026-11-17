import velis from 'eslint-config-velis';

export default [
  ...velis,
  {
    ignores: ['.venv/*', '**/dist/*', 'coverage/*', 'node_modules/*', 'docs/*', 'demo/frontend/vite.config.ts'],
  },
  {
    files: ['demo/frontend/**/*.ts', 'demo/frontend/**/*.vue'],
    languageOptions: { parserOptions: { project: './demo/frontend/tsconfig.json' } },
    rules: {
      // Vue single-file components use PascalCase file names.
      'unicorn/filename-case': 'off',
    },
  },
  {
    files: ['demo/e2e/**/*.ts'],
    languageOptions: { parserOptions: { project: './demo/e2e/tsconfig.json' } },
  },
];
