# Third-party notices

The ccmeter dashboard bundles the following files, unmodified except where noted, so that it
loads nothing from other origins. Each keeps its own license; the full texts sit next to the
files in `src/ccmeter/server/static/`.

| Component | Version | Files | License | Source |
|---|---|---|---|---|
| Preact | 10.29.8 | `vendor/preact.module.js` | MIT, `vendor/preact.LICENSE.txt` | https://github.com/preactjs/preact (npm `preact`) |
| htm | 3.1.1 | `vendor/htm.module.js` | Apache-2.0, `vendor/htm.LICENSE.txt` | https://github.com/developit/htm (npm `htm`) |
| Space Grotesk | variable, latin subset | `fonts/SpaceGrotesk-latin.woff2` | SIL Open Font License 1.1, `fonts/SpaceGrotesk.OFL.txt` | https://github.com/floriankarsten/space-grotesk |
| JetBrains Mono | variable, latin subset | `fonts/JetBrainsMono-latin.woff2` | SIL Open Font License 1.1, `fonts/JetBrainsMono.OFL.txt` | https://github.com/JetBrains/JetBrainsMono |

Notes:
- Preact and htm are the `dist/*.module.js` files of the npm packages, checked against the
  registry's sha512 integrity. The trailing `sourceMappingURL` comment was removed from
  `preact.module.js` because the source map is not shipped.
- The fonts are the latin subsets served by Google Fonts for these families, unmodified.
