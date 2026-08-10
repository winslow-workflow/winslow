# Changelog

All notable changes to Winslow are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Winslow follows [Semantic Versioning](https://semver.org/) (pre-1.0: minor
versions may include breaking changes).

## [Unreleased]

## [0.3.0] — 2026-08-10

### Added

- Error telemetry: errors report to Sentry (`winslow[sentry]`) and OpenTelemetry
  (`winslow[otel]`) when their environment values are set, with a `telemetry.py`
  seam for custom backends. See the telemetry docs.
- Settings resolve through python-decouple: a `.env` file in the working
  directory (or a parent) now works.

### Changed

- Log records carry the name and the instance of the workflow and the task.

## [0.2.0] — 2026-07-30

Initial public release.

Note: 0.1.0 was a placeholder upload that reserved the package name on PyPI.
0.2.0 is the first real release.
