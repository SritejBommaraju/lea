"""Lea -> Grain: a finished scan, its findings and its measurements become a linked Grain project that designs the
repair part (Phase 2, PLAN.md §9).

    manifest.py  the capture manifest (LEA_CAPTURE.json, schema 1) and the first design message, built from the scan's
                 report, stills, mesh and measurement record. Pure: no service is called.
    service.py   the durable handoff (STATE row `grain-handoff`): create or reuse the project, upload the scan and the
                 chosen photos, write the manifest, start one detached design turn, poll, relay Grain's questions, and
                 keep each turn's final build as an immutable revision. Every step is persisted before the next, and a
                 retry reconciles with Grain before calling again, so a retried request never makes a second project
                 or a second turn.

Only Grain's API surface is used (A10): project, scan, photo, file, chat, message and report routes.
"""
