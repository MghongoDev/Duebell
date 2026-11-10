# Vendored scripts and styles

Duebell serves these third-party files itself, so the content security policy
can allow scripts from this app alone (issue #49). They are byte-for-byte copies
of the published files. Do not edit them. To upgrade, download the new version
from the same source, replace the folder, and update this table.
`tests/test_csp.py` checks each file against its hash.

| File | Version | Source | SHA-256 |
| --- | --- | --- | --- |
| `ag-grid-community-32.3.3/ag-grid-community.min.js` | 32.3.3 | https://cdn.jsdelivr.net/npm/ag-grid-community@32.3.3/dist/ag-grid-community.min.js | `9454efe8fb5be3348b4d273ffc87f9079c1eed9a46141b25d3be450cf87d2d45` |
| `ag-grid-community-32.3.3/ag-grid.css` | 32.3.3 | https://cdn.jsdelivr.net/npm/ag-grid-community@32.3.3/styles/ag-grid.css | `c623f89cee588e33da1ab100b9c8cfe21c98340a6c22ee492908c4aa7da0f9f5` |
| `ag-grid-community-32.3.3/ag-theme-quartz.css` | 32.3.3 | https://cdn.jsdelivr.net/npm/ag-grid-community@32.3.3/styles/ag-theme-quartz.css | `836a7fad16bdea17e3ffd9dbf473d83add52ddad7e80317b4bdd46e00d841002` |
| `htmx-2.0.3/htmx.min.js` | 2.0.3 | https://unpkg.com/htmx.org@2.0.3 (redirects to `/dist/htmx.min.js`) | `491955cd1810747d7d7b9ccb936400afb760e06d25d53e4572b64b6563b2784e` |

Downloaded on 2026-11-10. AG Grid Community is MIT licensed; htmx is under the
Zero-Clause BSD licence.
