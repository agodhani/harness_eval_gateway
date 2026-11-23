---
slug: support-reply
title: Draft customer support replies
description: Write empathetic, policy-accurate support replies to customer tickets in the workspace.
category: support
tags: [writing, support, communication]
suggested_adapter: miniloop
suggested_rubric: support-tone-compliance
---

The workspace contains customer support tickets plus a policy/knowledge-base document (e.g. refund rules, SLAs, product facts).

Your task:

1. Read the policy document(s) first — every claim in your replies must be consistent with them.
2. For each ticket, draft a reply saved as `replies/<ticket-id>.md`:
   - Acknowledges the customer's specific issue in plain, empathetic language (no robotic "we apologize for any inconvenience").
   - Gives the correct answer or resolution path per the policy — exact refund amounts, timelines, and steps where the policy provides them.
   - Says honestly what is NOT covered when the policy rules it out, and offers the best available alternative.
   - States clearly when human escalation is needed rather than inventing an answer.
3. Tone: professional, warm, concise — no jargon the customer wouldn't know, no promises beyond policy.
4. Write `SUMMARY.md`: one line per ticket — issue type, the resolution offered, and whether escalation is recommended.

Deliverables: `replies/*.md` plus `SUMMARY.md`. Do not modify the tickets or policy files.
