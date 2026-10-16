# INT-2026-10-16-C inactive containment candidate

This package is dedicated to cron job `f10749396cea`. It is source-only and deliberately cannot be
activated. It does not alter the gateway, cron store, timer, live config, identities, firewall, or
systemd state.

## Deliberate activation blockers

- no `[Install]` section;
- `ConditionPathExists` points to a nonexistent second-authorization sentinel;
- the package expects an uninstalled `/opt/hermes-int-c` payload and `hermes-int-c` identity;
- executable digests and both IPv4/IPv6 address allowlists contain invalid `@@...@@` placeholders;
- the endpoint policy contains invalid literal-IP/CIDR placeholders.

A separately authorized installer must resolve endpoint addresses, render exact IPv4 and IPv6 CIDRs,
materialize a private read-only payload, bind hashes for `/usr/bin/env`, `/usr/bin/python3`, and
`/usr/bin/sha256sum`, create a non-root identity, and independently validate the effective unit. A
hostname is not an OS egress boundary: rendered systemd CIDRs must match the policy's fixed connect
addresses.

## Runtime contract

The worker accepts only closed-schema batches for `f10749396cea`, reads them descriptor-relatively
beneath `/var/lib/hermes-int-c/input`, rejects symlinks/traversal/hard links, performs fixed HTTPS GETs
without DNS, redirects, request bodies, arbitrary paths, environment-derived input, credentials, or
subprocesses, and emits only status/size/content digest metadata. It never prints response bodies.

`ProtectSystem=strict`, `ProtectHome=yes`, explicit read-only paths, empty capabilities, syscall denial,
`IPAddressDeny=any`, explicit IPv4+IPv6 placeholders, and CPU/memory/swap/task/runtime quotas are
packaged but remain **unverified** until a second authorization permits disposable real-systemd
installation and behavioral testing. Static checks must not be reported as enforcement.

## Test command

```sh
scripts/run_tests.sh tests/ops/test_int_c_containment.py
```

Promotion is blocked while any required control is `observed` or `unverified`.
