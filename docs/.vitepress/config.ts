import { defineConfig } from 'vitepress';
import { crosslinksConfig } from 'vitepress-plugin-crosslinks';

export default defineConfig({
  title: 'adorm',
  description:
    'Async ORM interface for Python: build queries and model operations with one async API, executed by pluggable drivers for the Django ORM and PostgreSQL.',
  // the directory's own README, for contributors
  srcExclude: ['readme.md'],
  // Icon and canonical URL under the `--base` given to the build; VitePress does not prefix `head` entries.
  transformHead({ pageData, siteConfig }) {
    const base = siteConfig.site.base;
    const path = pageData.relativePath.replace(/(^|\/)index\.md$/, '$1').replace(/\.md$/, '.html');
    return [
      ['link', { rel: 'icon', type: 'image/svg+xml', href: `${base}adorm-icon.svg` }],
      ['link', { rel: 'canonical', href: `https://docs.velis.si${base}${path}` }],
    ];
  },
  markdown: {
    // Override a base with CROSSLINKS_BASE_<NAME>, e.g. CROSSLINKS_BASE_MUXWS.
    config: crosslinksConfig({
      projects: {
        docs_velis: 'https://docs.velis.si',
        fastapi_viewsets: 'https://docs.velis.si/dynamicforms/fastapi-viewsets',
        muxws: 'https://docs.velis.si/muxws',
      },
    }),
  },
  themeConfig: {
    logo: '/adorm-icon.svg',
    nav: [
      { text: 'Home', link: '/' },
      { text: 'Introduction', link: '/intro/' },
      { text: 'Topics', link: '/topics/concepts' },
      { text: 'How-to', link: '/howto/' },
      { text: 'Reference', link: '/ref/' },
    ],
    // Sections follow the Django documentation: introduction, topic guides, how-to guides, reference.
    sidebar: [
      {
        text: 'Introduction',
        items: [
          { text: 'What is adorm', link: '/intro/' },
          { text: 'Changelog', link: '/intro/changelog' },
        ],
      },
      {
        text: 'Topics',
        items: [{ text: 'Concepts', link: '/topics/concepts' }],
      },
      {
        text: 'How-to guides',
        items: [{ text: 'How-to guides', link: '/howto/' }],
      },
      {
        text: 'Reference',
        items: [{ text: 'API reference', link: '/ref/' }],
      },
    ],
    socialLinks: [{ icon: 'github', link: 'https://github.com/velis74/adorm' }],
    footer: { message: 'Released under the MIT License.', copyright: 'Copyright © 2026 Jure Erznožnik' },
    search: { provider: 'local' },
  },
  // Only localhost links are ignored, so a dead internal link fails the build.
  ignoreDeadLinks: [/^http:\/\/localhost/],
});
