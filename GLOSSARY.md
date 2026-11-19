# pytools

Shared plumbing for midnightodyssey projects. These terms name the operator-facing safety
concepts the library provides, so consuming projects use the same words.

## Language

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
