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
          { text: 'Demo application', link: '/intro/demo' },
        ],
      },
      {
        text: 'Topics',
        items: [
          { text: 'Concepts', link: '/topics/concepts' },
          { text: 'Models', link: '/topics/models' },
          { text: 'Making queries', link: '/topics/queries' },
          { text: 'Managers and driver methods', link: '/topics/managers' },
          { text: 'Sessions and bound models', link: '/topics/sessions' },
          { text: 'Transactions', link: '/topics/transactions' },
          { text: 'Connection profiles', link: '/topics/profiles' },
          { text: 'Pool and router', link: '/topics/pool' },
          { text: 'Events', link: '/topics/events' },
          { text: 'Drivers', link: '/topics/drivers' },
        ],
      },
      {
        text: 'How-to guides',
        items: [
          { text: 'How-to guides', link: '/howto/' },
          { text: 'Multiple database users', link: '/howto/multiple-database-users' },
          { text: 'Writing a driver', link: '/howto/writing-a-driver' },
          { text: 'Generating models', link: '/howto/generating-models' },
        ],
      },
      {
        text: 'Reference',
        items: [
          { text: 'API reference', link: '/ref/' },
          { text: 'QuerySet API', link: '/ref/queryset' },
          { text: 'Query', link: '/ref/query' },
          { text: 'Model API', link: '/ref/models' },
          { text: 'Operations', link: '/ref/operations' },
          { text: 'Operation encoding', link: '/ref/encoding' },
          { text: 'Lookups', link: '/ref/lookups' },
          { text: 'Expressions', link: '/ref/expressions' },
          { text: 'Sessions', link: '/ref/sessions' },
          { text: 'Transactions', link: '/ref/transactions' },
          { text: 'Pool and router', link: '/ref/pool' },
          { text: 'Settings', link: '/ref/settings' },
          { text: 'Exceptions', link: '/ref/exceptions' },
          { text: 'Drivers', link: '/ref/drivers' },
          { text: 'Events', link: '/ref/events' },
          { text: 'Model generator', link: '/ref/codegen' },
        ],
      },
    ],
    socialLinks: [{ icon: 'github', link: 'https://github.com/velis74/adorm' }],
    footer: { message: 'Released under the MIT License.', copyright: 'Copyright © 2026 Jure Erznožnik' },
    search: { provider: 'local' },
  },
  // Only localhost links are ignored, so a dead internal link fails the build.
  ignoreDeadLinks: [/^http:\/\/localhost/],
});
