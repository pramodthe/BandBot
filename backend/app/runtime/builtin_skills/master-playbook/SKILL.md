---
name: master-playbook
description: Choose native components or a sandboxed interactive answer for explanations, comparisons, calculators, and simulations.
---
# Interactive answer playbook

Adapted from CopilotKit/OpenIntelligentUI's master-playbook. See the bundled provenance and MIT license.

Answer the actual task. Use text for direct facts, writing, code, and simple advice.
Use `ui_catalog` and `present_ui` for tables, charts, maps, comparisons and other supported native displays.
Only use `generate_sandboxed_ui` when the current human request explicitly asks for a custom/interactive interface, calculator, simulation or that tool. Relevance alone does not opt in.
An ordinary explanation request must stay text or use the native catalogue. Never use a computer/app server to bypass this policy.
BAND_BOTS's answer model chooses the presentation; there is no Jev router or CopilotKit runtime to call.
Do not turn every answer into a dashboard or generate a second widget after a successful tool result.

- Understand: show a mechanism or relationship, with a meaningful variable or stepper when useful.
- Compare: show criteria, assumptions, units and tradeoffs. Use a native table for simple cases.
- Make a tool: deliver working inputs, validated calculations, outputs and a reset control.

## Evidence and persistence

Use user-provided data, conversation documents, retrieved evidence and verified calculations.
Distinguish those from sample assumptions inside the UI. Rendering never establishes a live data connection.
Never invent current weather, prices, business metrics, citations, bookings or successful external actions.
There is no `query_data` sample database in BAND_BOTS. Use the available scoped document and research tools.
If required inputs are missing, use `ask_user`; it pauses and resumes the same task durably.
External actions still require `request_approval` and channel policy checks.

`generate_sandboxed_ui` saves one interface with the completed reply. The channel's Views can reopen it.
Repeated calls within one task replace that draft's source; they do not add cards. Combine sections in one interface.
Local control state lasts for that iframe session, not across reloads or devices. A requested follow-up revision creates one new reply.
Use `read_views` before answering questions about an existing interface. Never claim to patch a previous iframe.

## Tool contract

Emit `initialHeight`, `placeholderMessages`, `css`, `html`, `jsFunctions`, `jsExpressions` in that order.
Provide a useful title and optional description. `initialHeight` is 240–1000 pixels; the complete payload is at most 128 KB.
`html` is a body fragment: no document wrappers, policy tags, frames, objects or embeds.
Put styles in `css`, named behavior declarations in `jsFunctions`, and small initialization calls in `jsExpressions`.
The two JavaScript channels are classic scripts: no static imports or top-level await. Dynamic imports belong inside async functions.
The interface appears when this tool's result is attached to the completed reply; do not promise incremental HTML streaming.

The opaque-origin browser sandbox runs this code, not the API host or E2B computer.
It has no workspace cookies, storage, parent DOM or backend fetch access. Never collect credentials.
The host provides theme variables and an import map for `three`, `three/addons/`, `d3`, `chart.js`, and `gsap`.
Read advanced-visualization for implementation and network limits.

## Human-reviewed follow-ups

Sliders, sorting, filters, tabs, calculations and animation controls stay local and must work without another model call.
A clearly labelled button may call `await BandUI.sendPrompt({ text })` with validated current selections.
This PROPOSES a follow-up outside the iframe. The human reviews/edits the text and clicks **Send to agent**.
The promise resolves only after host confirmation. Cancellation or failure rejects: catch it and show a retryable status.
For a link use `await BandUI.openLink({ url })` with an HTTPS URL; the host asks the human to open it.
`Websandbox.connection.remote` is a compatibility alias for these same reviewed methods, not a privilege bypass.
Never send on load, timers or ordinary input changes. Never include secrets. A follow-up is not external-action approval.
The sandbox cannot call arbitrary tools or choose a different agent, channel, endpoint, task or approval decision.

```js
async function askAboutScenario(button) {
  const status = document.getElementById("followup-status");
  button.disabled = true;
  status.textContent = "Review the proposed message outside this interface.";
  try {
    const amount = document.getElementById("amount").valueAsNumber;
    if (!Number.isFinite(amount) || amount < 0) throw new Error("Invalid amount");
    await BandUI.sendPrompt({ text: "Explain this scenario with amount " + amount + "." });
    status.textContent = "Follow-up sent.";
  } catch {
    status.textContent = "Not sent. Check the inputs or try again.";
  } finally {
    button.disabled = false;
  }
}
```

Use semantic headings, associated labels, keyboard controls, visible focus and readable error/status text.
Validate blanks, finite numbers, domain limits and zero divisors. Show units and formulas. Never display stale, NaN or infinite results as valid.
Keep source text out of executable HTML interpolation: use `textContent`. Respect reduced motion and phone layouts.
