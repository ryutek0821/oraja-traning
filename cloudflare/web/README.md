# Web module

The static assets implement the issue #15 account and dashboard surface. They
use only same-origin authenticated APIs, derive the single profile from the
session-owned `/v1/dashboard` response, and never accept a free-form profile or
capability identifier. Japanese and Korean catalogs cover the primary account,
device, privacy, and IR setup flows. Device tokens and recovery codes are shown
once and cleared when their dialogs close.
