# pytools

Shared plumbing for midnightodyssey projects. These terms name the operator-facing safety
concepts the library provides, so consuming projects use the same words.

## Language

### Kill switch

**Kill switch**:
An operator-controlled signal that, while armed, forbids every gated action in the processes that consult it.
_Avoid_: Halt file, halt switch, stop file

**Armed**:
The state of a kill switch in which gated actions are forbidden; any doubt about the state counts as armed.
_Avoid_: Halted, tripped, on

**Disarmed**:
The state of a kill switch in which gated actions may proceed.
_Avoid_: Resumed, cleared, off

**Arm / Disarm**:
The only two transitions of a kill switch.
_Avoid_: Halt / resume, trip / reset

### Notifications

**Notification**:
A message telling operators that something happened, delivered on a best-effort basis: a failure to deliver one never changes what the sending process does next.
_Avoid_: Alert, notice, ping

**Severity**:
How urgently a notification needs an operator's attention: `info`, `warn` or `critical`, in rising order.
_Avoid_: Level, priority, urgency

**Critical**:
The highest severity, reserved for events an operator must act on, such as a kill switch being armed.
_Avoid_: Alert, error, urgent

**Destination**:
A named place notifications are posted to, such as `chief-of-staff`; each destination is one Slack webhook.
_Avoid_: Channel, room, target
