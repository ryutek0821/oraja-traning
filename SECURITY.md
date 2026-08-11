# Security policy

Security reports must not include play history, database files, tokens, or
other personal data. Open a private security advisory or contact the security
maintainer listed in the repository settings. Do not publish an exploit before
the maintainer acknowledges receipt.

Supported versions are the latest tagged release and the current default
branch. Reports are triaged by impact: credential disclosure, cross-profile
access, raw-data exposure, and remote code execution are critical; denial of
service and non-sensitive integrity issues are lower priority.

The service never accepts BMS bodies, replay key input, AI provider keys, or
raw passwords in logs. Suspected token compromise must be handled by revoking
the device/session and rotating the affected secret.
