"""AppStore service layer.

Installation is implemented in `api/routers/apps.py` (built-in catalog under
`aegis/server/appstore/catalog` + the `aegis-appstore` repo, executed via the
omodul dispatcher / docker compose). The old oservice `AppInstallerEngine`
assembly lived here but had no caller and required a remote catalog URL that is
never configured; it was removed rather than left as a second, dead install path.
"""
