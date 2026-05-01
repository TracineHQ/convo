# Security policy

## Supported versions

The latest released minor version receives security fixes.

## Reporting a vulnerability

Do not open a public GitHub issue. Email security reports to
`anthonymledesma@gmail.com` with the subject line `convo security report`.

Please include:

- A description of the issue and its potential impact.
- Steps to reproduce, ideally with a minimal proof of concept.
- Any suggested mitigations.

You can expect:

- An acknowledgement within 72 hours.
- A coordinated disclosure timeline once the issue is confirmed.
- Public credit in the release notes if you would like it.

## What's in scope

- The `convo` Python package and CLI.
- The CI configuration and pre-commit hooks shipped in this repo.

## What's out of scope

- Issues in upstream dependencies (file those upstream and link the
  advisory here once available).
- The contents of users' local Claude Code session files.
