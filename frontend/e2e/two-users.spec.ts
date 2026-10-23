import { expect, test, type Page } from "@playwright/test";

async function signup(page: Page, username: string, displayName: string) {
  await page.goto("/");
  await page.getByRole("button", { name: "New here? Create an account" }).click();
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Display name").fill(displayName);
  await page.getByLabel("Password").fill("secret123");
  await page.getByLabel("Band human API key").fill(`band_u_${username}`);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByRole("button", { name: /^People/ })).toBeVisible();
}

async function showList(page: Page) {
  const dialog = page.getByRole("dialog");
  if (await dialog.isVisible()) {
    await dialog.getByRole("button", { name: "Close" }).click();
  }
  const back = page.getByRole("button", { name: "Back to conversations" });
  if (await back.isVisible()) await back.click();
}

async function send(page: Page, text: string) {
  const box = page.getByRole("combobox", { name: "Message" });
  await expect(box).toBeVisible();
  await box.fill(text);
  const submit = page.getByRole("button", { name: "Send" });
  await expect(submit).toBeEnabled();
  await submit.click();
}

async function authed(page: Page, path: string, method: string, body?: unknown) {
  return page.evaluate(async ({ path, method, body }) => {
    const token = localStorage.getItem("band-slack-token");
    const response = await fetch(path, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: response.status, text: await response.text() };
  }, { path, method, body });
}

test("two people consent, talk, invite, and reach a workspace agent", async ({ browser }, testInfo) => {
  const suffix = testInfo.project.name === "compact" ? "c" : "d";
  const aliceName = `alice${suffix}${Date.now().toString().slice(-4)}`;
  const bobName = `bob${suffix}${Date.now().toString().slice(-4)}`;
  const aliceDisplay = `Alice ${suffix}`;
  const bobDisplay = `Bob ${suffix}`;
  const agentName = suffix === "c" ? "Aide" : "Helper";
  const channelName = `plan${suffix}`;
  const alice = await browser.newContext();
  const bob = await browser.newContext();
  const alicePage = await alice.newPage();
  const bobPage = await bob.newPage();

  await signup(alicePage, aliceName, aliceDisplay);
  await signup(bobPage, bobName, bobDisplay);

  await alicePage.getByRole("button", { name: /^People/ }).click();
  await alicePage.getByLabel("Find a person by app username").fill(bobName);
  await alicePage.getByRole("button", { name: "Send request" }).click();
  await expect(bobPage.getByRole("button", { name: /People, 1 pending/ })).toBeVisible();
  await bobPage.getByRole("button", { name: /People, 1 pending/ }).click();
  await bobPage.getByRole("button", { name: "Accept", exact: true }).click();
  await expect(alicePage.getByRole("button", { name: "Message", exact: true })).toBeVisible();
  await alicePage.getByRole("button", { name: "Message", exact: true }).click();
  await expect(alicePage.getByText(`Chat with ${bobDisplay}`)).toBeVisible();
  await send(alicePage, "hello from alice");
  await showList(bobPage);
  await expect(bobPage.getByRole("button", { name: aliceDisplay })).toBeVisible();
  await bobPage.getByRole("button", { name: aliceDisplay }).click();
  await expect(bobPage.getByText("hello from alice")).toBeVisible();
  await send(bobPage, "hello from bob");
  await expect(alicePage.getByText("hello from bob")).toBeVisible();

  await showList(alicePage);
  await alicePage.getByRole("button", { name: "New channel", exact: true }).click();
  const createChannel = alicePage.getByRole("dialog");
  await createChannel.getByRole("textbox", { name: "Name", exact: true }).fill(channelName);
  await createChannel.getByRole("button", { name: "Create", exact: true }).click();
  await alicePage.getByRole("button", { name: "Channel members", exact: true }).click();
  await alicePage.getByRole("button", { name: "Invite people" }).click();
  await alicePage.getByRole("dialog").getByRole("button", { name: "Invite", exact: true }).click();
  await alicePage.getByRole("dialog").getByRole("button", { name: "Close" }).click();
  const boot = JSON.parse((await authed(alicePage, "/api/bootstrap", "GET")).text) as { channels: { id: number; name: string }[] };
  const created = boot.channels.find((item) => item.name === channelName);
  expect(created).toBeTruthy();
  const denied = await authed(bobPage, `/api/channels/${created!.id}/messages`, "GET");
  expect(denied.status).toBe(404);

  await showList(bobPage);
  await expect(bobPage.getByText(`Invitation from ${aliceDisplay}`)).toBeVisible();
  await bobPage.getByText(`Invitation from ${aliceDisplay}`).click();
  await bobPage.getByRole("button", { name: "Join channel" }).click();
  await showList(bobPage);
  await bobPage.getByRole("button", { name: channelName }).click();

  const agent = await authed(alicePage, "/api/agents", "POST", {
    name: agentName, role: "custom", instructions: "", channel_id: created!.id,
  });
  expect(agent.status).toBe(200);
  const agentId = JSON.parse(agent.text).id as number;
  const privateDm = await authed(bobPage, `/api/agents/${agentId}/dm`, "POST");
  expect(privateDm.status).toBe(403);

  await bobPage.getByRole("button", { name: "Channel members", exact: true }).click();
  const members = bobPage.locator("aside").filter({ has: bobPage.getByRole("heading", { name: "Channel members" }) });
  await expect(members.getByRole("button", { name: agentName })).toBeVisible();
  await expect(members.getByRole("button", { name: "Remove from channel" })).toHaveCount(0);
  await bobPage.getByRole("button", { name: "Close channel members" }).click();
  await send(bobPage, `@${agentName} please help`);
  await expect(bobPage.getByText("I can help with that.")).toBeVisible();

  await bobPage.getByRole("button", { name: "Channel members", exact: true }).click();
  await bobPage.getByRole("button", { name: "Leave channel" }).click();
  await expect.poll(async () => (await authed(bobPage, `/api/channels/${created!.id}/messages`, "GET")).status).toBe(404);

  await showList(alicePage);
  await alicePage.getByRole("button", { name: "Add agents", exact: true }).click();
  await alicePage.getByRole("button", { name: /Cursor/ }).click();
  await expect(alicePage.getByRole("status").filter({ hasText: "Reconnect the workspace before adding this agent." })).toBeVisible();
  await expect(alicePage.getByRole("heading", { name: "Create agent" })).toHaveCount(0);
  await alicePage.getByRole("button", { name: "Cancel" }).click();
  await alicePage.getByRole("button", { name: "Add agents", exact: true }).click();
  await alicePage.getByRole("button", { name: /Cursor/ }).click();
  await alicePage.getByRole("button", { name: "All agents" }).click();
  await expect(alicePage.getByRole("heading", { name: "Add agents" })).toBeVisible();
  await alicePage.getByRole("button", { name: "Close" }).click();

  const second = await authed(bobPage, "/api/channels", "POST", { name: `later${suffix}` });
  expect(second.status).toBe(200);
  const laterId = JSON.parse(second.text).id as number;
  const aliceBoot = JSON.parse((await authed(alicePage, "/api/bootstrap", "GET")).text) as { me: { id: number } };
  await alicePage.context().setOffline(true);
  const invited = await authed(bobPage, `/api/channels/${laterId}/invitations`, "POST", { user_id: aliceBoot.me.id });
  expect(invited.status).toBe(200);
  await alicePage.context().setOffline(false);
  await showList(alicePage);
  await expect(alicePage.getByText(`Invitation from ${bobDisplay}`)).toBeVisible();

  await alicePage.getByRole("button", { name: channelName, exact: true }).click();
  const closeMembers = alicePage.getByRole("button", { name: "Close channel members" });
  if (await closeMembers.isVisible()) await closeMembers.click();
  await expect(alicePage.getByRole("combobox", { name: "Message" })).toBeVisible();
  await alicePage.context().setOffline(true);
  await alicePage.getByRole("combobox", { name: "Message" }).fill("retry me once");
  await alicePage.getByRole("button", { name: "Send" }).click();
  await expect(alicePage.getByRole("button", { name: "Retry sending" })).toBeVisible();
  await alicePage.context().setOffline(false);
  await alicePage.getByRole("button", { name: "Retry sending" }).click();
  await expect(alicePage.getByText("retry me once")).toHaveCount(1);

  await alice.close();
  await bob.close();
});
