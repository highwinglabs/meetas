# Security

This project treats the security and privacy of your data as first-class concerns. The
application is local-first: it binds to `127.0.0.1` only, sends no network traffic by
default, and performs no telemetry or analytics.

## Reporting a vulnerability

Please do **not** open a public issue for a security vulnerability. Use the repository's
**Security → Advisories → New advisory** page on GitHub to file a private report so it
can be fixed quietly.

Include, when available: a short description of the issue, the affected version, and
steps to reproduce.

## Response

- We acknowledge valid reports as soon as possible and aim to ship a fix in the next
  release.
- We publish a summary once a fix is available (with credits unless you prefer to
  remain anonymous).

## Scope

In scope: the REST API, the daemon/lock handling, backup/restore, file and directory
permissions, and the keyring-backed secret store.

Out of scope: vulnerabilities in third-party dependencies (report them to the
respective upstream project) and misconfigurations that require the user to explicitly
expose the loopback endpoint publicly.
