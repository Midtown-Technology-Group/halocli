# Friday Present For You

Friend,

Here is a small HaloPSA CLI that came out of our agent-speedup work. It is
standalone, JSON-first, and deliberately boring in the places that should be
boring: direct OAuth client-credentials auth, bounded pagination, clear error
categories, and a guarded raw request escape hatch.

There is also a safe SSO discovery path now:

```powershell
halocli auth discover --tenant-url https://yourtenant.halopsa.com
```

That command does not store secrets unless you explicitly add `--profile haloagent
--save`. It checks whether your Halo instance looks like it supports a
CLI-friendly browser callback flow before anyone tries to use Entra SSO from the
command line. If discovery does not confirm it, stick with client-credentials
for automation or plan an Entra-backed broker.

It is not wired to Bifrost and it does not need our workspace runtime. The idea
is that you can use it as a tiny operator tool, hand it to automation, or remix
the shape into whatever the next Halo tenant needs.

Quick start:

```powershell
python -m pip install -e ".[dev]"
halocli configure --profile haloagent --auth-mode halo-interactive
halocli auth discover --tenant-url https://yourtenant.halopsa.com --profile haloagent --save
halocli auth login --profile haloagent
halocli auth test
halocli clients list --param search=Example
halocli tickets list --open --max-records 25
```

Register this redirect URL on the Halo API application:

```text
http://127.0.0.1:8765/callback
```

Raw writes are intentionally guarded:

```powershell
halocli raw POST /Tickets --data payload.json --apply --yes
```

Happy Friday.
