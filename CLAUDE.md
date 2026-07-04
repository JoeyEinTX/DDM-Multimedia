# DDM Multimedia — Claude Code project instructions

This repo is connected to Joey's Context Vault.

## First thing to read

Before making changes, read the current project state:

- `D:\vault\projects\ddm-multimedia\status.md`
- `D:\vault\wiki\ddm-multimedia.md`

Also skim the vault schema:

- `D:\vault\CLAUDE.md`

## Project role

DDM Multimedia is an active multimedia/game/event project repo. Treat current source state carefully because it may contain active prototype work, hardware sketches, and local configuration.

## Rules

1. Check `git status --short` before editing.
2. Treat the vault status page as more current than old README/docs.
3. Do not touch existing dirty files unless Joey explicitly asks.
4. Do not commit secrets, Wi-Fi credentials, tokens, passwords, or local machine config.
5. Do not make broad rewrites without a plan.
6. Do not commit unless Joey explicitly says to commit.
7. After meaningful work, tell Joey what should be updated in the vault.

## Known caution

If `esp32/ddm_led_controller/config.h` is dirty, assume it may contain local hardware/network configuration. Inspect carefully and do not commit it unless Joey explicitly confirms it is sanitized.

Ignore `__pycache__` noise unless Joey asks to clean it.

## End of session

Report:

- what changed
- tests run
- files modified
- whether anything should be added to the vault
- exact next step
