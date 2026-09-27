# Third-party notices

## python-garminconnect

Garmin Training Lab depends on [python-garminconnect](https://github.com/cyberjunky/python-garminconnect), created and maintained by Ron Klinkien and contributors. It is an unofficial Garmin Connect client. This project uses it as a dependency and adds a separate collection and analysis workflow; it does not claim authorship of the upstream library or affiliation with Garmin.

Pinned source revision: [`c3c1c0d66579696e3843cba20f985c66069140b9`](https://github.com/cyberjunky/python-garminconnect/tree/c3c1c0d66579696e3843cba20f985c66069140b9).

The following is the upstream MIT license from that revision:

```text
MIT License

Copyright (c) 2020-2026 Ron Klinkien

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
```

## Other tools and references

Codex CLI is installed separately and authenticates through the user's own ChatGPT account. Its source, distribution terms, and service terms are separate from this repository's MIT license.

Other installed dependencies retain their own licenses and notices. Direct dependencies are declared in `pyproject.toml`; `uv.lock` records the resolved environment. TrainingPeaks, Strava, V.O2, Garmin documentation, and research papers are linked as methodological sources, not bundled libraries or endorsements.
