# Third-Party Notices

HaloCLI is original Midtown Technology Group work released under the GNU
General Public License v3.0. See `LICENSE`.

## Prior Art: `netaryx/pyhalopsa`

HaloCLI was inspired by the public `netaryx/pyhalopsa` project:

- Repository: https://github.com/netaryx/pyhalopsa
- Author/maintainer credit: `dmurray14` / Netaryx
- Package name in upstream metadata: `halopsa`
- Declared upstream license: MIT, as listed in upstream `pyproject.toml`

We did not vendor `pyhalopsa`, add it as a dependency, or intentionally copy its
source code into HaloCLI. The ideas we used as prior art were the broad product
shape: a Python HaloPSA SDK/CLI, resource-oriented command surfaces,
pagination helpers, error handling, and raw-request escape hatches.

Thank you to `dmurray14` and Netaryx for publishing that work.

### MIT License Notice

The upstream project declares the MIT license in package metadata. As of the
initial HaloCLI public release, the upstream repository did not expose a root
license file in the GitHub contents API, so this notice records the declared
SPDX-style license from `pyproject.toml` rather than reproducing a project-local
license file.

Standard MIT license text:

```text
MIT License

Copyright (c) <year> <copyright holders>

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
