# Provisioning Hetzner instances

Optional and provider-specific. Nothing in the platform depends on it — a machine created by hand in the console is identical. It exists because the instance types this deployment wants are routinely out of stock, and because two properties of creating one correctly are worth encoding rather than remembering.

**The location must be inside the EEA.** Both hosts store personal data — the application host holds accounts and an action log, the evidence host holds security events carrying IP addresses and user ids, plus backup archives. An EEA location is what keeps a privacy notice's international-transfer answer "none"; anywhere else turns it into a question needing a transfer mechanism. The script refuses a non-EEA location unless `ALLOW_NON_EEA=1` is set, because the failure is otherwise silent: a server in Ashburn works exactly as well as one in Helsinki, and the only thing that changes is a document nobody re-reads.

**The firewall goes on in the create call.** A server created without one answers SSH from the whole internet between boot and whatever second API call attaches it. Passing it at creation is the difference between a window and no window.

## One script, several machines

Each machine gets its own config file, because they differ in ways that matter:

| Machine | Config | Shape |
|---|---|---|
| Evidence host | `watch.env` (default) | 4 GB, **no inbound ports at all** — reached over the tailnet |
| Application host | `app-host.env` | 8 GB preferred, 80 and 443 open to the internet for ACME and the SPA |

```bash
cp watch.env.example watch.env      # then fill it in
./provision-hetzner.sh              # evidence host
./provision-hetzner.sh app-host.env # application host
```

Do not share one firewall between them. The evidence host's rules exist to make it unreachable; the application host's exist to make it reachable.

`*.env` here is gitignored — each holds a read+write API token that can create and destroy servers.

## Two things that bit us

**Pass every SSH key you will ever want.** Hetzner injects them at creation only. A key missing from that list can afterwards be added only from a session you still hold, and losing all of them means rescue mode. A working key plus a recovery key kept elsewhere is the minimum; a second administrator's key belongs there too if one is ever likely. The script warns when given only one.

**Server type names are perishable.** Hetzner renames its lines between generations, and the availability API skips a name it cannot resolve rather than rejecting it — so a stale list produces a watcher that polls indefinitely, reports nothing, and looks healthy. Every requested type is resolved at startup for that reason: unknown names warn individually, and if none survive the script refuses to start and prints the current names.
