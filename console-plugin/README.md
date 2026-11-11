# MikroTik RDS CSI OpenShift console plugin

This directory contains the OpenShift 4.22 dynamic-plugin frontend used by driver release `0.4.0`.

## Compatibility target

- OpenShift Console 4.22
- `@openshift-console/dynamic-plugin-sdk`: `4.22-latest`
- React 18
- PatternFly 6
- Webpack 5

The plugin contributes an Administrator-perspective navigation item under **Storage -> MikroTik RDS CSI** and loads status through the `status` `ConsolePlugin` proxy.

## Local frontend checks

Install dependencies:

```bash
npm install
```

Type-check:

```bash
npm run typecheck
```

Production build:

```bash
npm run build
```

The generated `dist/` directory is served by `Containerfile.console` through the Python status backend. `dist/` and `node_modules/` are intentionally ignored by git.

The repository currently does not include a generated `package-lock.json`; generate and commit one if you want fully locked npm dependency resolution for repeatable production builds, then change the container build to `npm ci`.

## Backend

Browser code calls only:

```text
/api/proxy/plugin/mikrotik-rds-csi-console/status/api/v1/overview
```

through `consoleFetch`. The OpenShift console proxy forwards the user's token to the in-cluster status service. RouterOS credentials are never part of this frontend bundle.

See the root `README.md` and `docs/console-plugin-roadmap.md` for deployment and architecture details.
