# Third-party notices

DesignAnalyzerBot combines original application code with open-source dependencies.

## WebAnalyze technology fingerprints

The production image copies `technologies.json` from `rverton/webanalyze` v0.4.5 and also copies its `LICENSE` to `/opt/webanalyze/LICENSE`.

MIT License

Copyright (c) 2020 Robin Verton

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Other runtime components

- berodcdev/designsys — MIT — design-system extraction.
- GoogleChrome/lighthouse — Apache-2.0 — performance, SEO and best-practice audits.
- axe-playwright-python — MIT — Python integration for axe-core.
- axe-core — MPL-2.0 — automated accessibility rules.
- webrecorder/warcio — Apache-2.0 — WARC writing.
- markdownify — MIT — HTML-to-Markdown conversion.
- readability-lxml — Apache-2.0 — readable-content extraction.
- Playwright — Apache-2.0 — browser automation.
- python-telegram-bot — LGPL-3.0 — Telegram Bot API client.
- Pillow — HPND — image processing.

Their own packages/images retain the license metadata distributed by their upstream projects.

## Not embedded

SingleFile and Browsertrix Crawler were researched but are not embedded in this project. Their AGPL licensing is therefore not introduced into DesignAnalyzerBot by this implementation.
