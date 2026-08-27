# Validation gates

This file is the canonical source for local and CI validation rules.

## Gates

- `mise run dev`: fast local validation for syntax and unit contracts.
- `mise run affected`: changed-file validation plus the unit-test fallback.
- `mise run release`: full repository release-readiness validation.

## Failure conditions

A gate fails when any checked Python file has invalid syntax, any checked
shell script fails `bash -n`, any unit test fails, required automation files are
missing, or a validation command exits non-zero.

## Scope and performance

Validation is deterministic and uses a documented no-cache fallback: Python
syntax is checked with `ast.parse`, so no bytecode cache is written. Shell
syntax uses `bash -n`. The affected gate checks changed Python and shell files
only, then runs unit tests as a safe fallback because this repository has no
coverage map yet.

## Local and GitHub parity

Local and GitHub validation both use `mise run release`. The workflow is
read-only and does not install, start, or contact ASR/LLM services. Python
minor versions may differ between local and GitHub runners; gates must remain
stdlib-only unless a future task pins tools in `mise.toml`.
