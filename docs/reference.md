# API reference

The public API of Winslow: the names that `import winslow` exposes, then the client API a UI pane
or an agent uses to read and drive sessions, in process or over the wire (see
[Serve and connect](serve.md) and [UI plugins](ui-plugins.md)).

## Workflow

::: winslow.Workflow

## Task

::: winslow.Task

## TaskFilter

::: winslow.TaskFilter

## Constraints

::: winslow.Constraint

::: winslow.ClassConstraint

::: winslow.ConstraintType

## Descriptors

::: winslow.Parameter

::: winslow.ConfigOption

## Decorators

::: winslow.decorators.transient_property

## Caching

::: winslow.cache.entry

::: winslow.cache.base.BaseCache

::: winslow.cache.GlobalCache

::: winslow.cache.WorkflowCache

::: winslow.cache.get_workflow_cache

::: winslow.cache.get_global_cache

::: winslow.cache.BaseStorage

::: winslow.cache.MemoryStorage

::: winslow.cache.JsonFileStorage

::: winslow.cache.compose

## Telemetry

::: winslow.telemetry.TelemetryConfiguration

::: winslow.telemetry.TelemetryHandler

## Orchestrator

::: winslow.Orchestrator

## Client API

::: winslow.client.AppClient

::: winslow.client.SessionClient

::: winslow.client.LocalAppClient

::: winslow.exceptions.RequestError

## Actions

::: winslow.actions

## Events

::: winslow.bus.SessionBus

::: winslow.events

## Model

::: winslow.model

## Sessions

::: winslow.session.SessionRegistry

::: winslow.session.create_session
