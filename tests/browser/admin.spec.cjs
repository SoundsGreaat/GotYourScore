const { expect, test } = require("@playwright/test");
const { randomUUID } = require("node:crypto");
const { base, nextResponse, signIn, watchPage } = require("./harness.cjs");

test("admin tabs scroll on mobile and select only after a successful swap", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await signIn(page);
  await page.goto(`${base}/admin`);
  await expect(page.locator("#admin-prompts-root")).toBeVisible();
  const prompts = page.getByRole("tab", { name: "Prompts" });
  const recovery = page.getByRole("tab", { name: "Deleted Refund Checks" });
  await prompts.focus();
  await page.keyboard.press("End");
  await expect(recovery).toBeFocused();
  await expect(prompts).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel", { name: "Prompts" })).toBeVisible();
  await expect.poll(() => page.getByRole("tablist").evaluate((track) => track.scrollLeft)).toBeGreaterThan(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);

  await page.route("**/admin/partials/deleted-refund-check", (route) => route.fulfill({
    status: 503,
    json: { detail: "Temporarily unavailable." },
  }), { times: 1 });
  await page.keyboard.press("Enter");
  await expect(page.getByRole("alert").filter({ hasText: "Temporarily unavailable." })).toBeVisible();
  await expect(page.getByRole("tabpanel", { name: "Prompts" })).toBeVisible();
  await expect(page).toHaveURL(`${base}/admin`);
  await page.keyboard.press("Space");
  await expect(page.getByRole("tabpanel", { name: "Deleted Refund Checks" })).toBeVisible();
  await expect(page).toHaveURL(`${base}/admin#deleted-refund-check`);
});

test("admin section tabs follow the layout's keyboard direction and restore deep links", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 780 });
  await signIn(page);
  await page.goto(`${base}/admin#users`);
  const tabs = page.getByRole("tablist", { name: "Admin sections" });
  const active = tabs.getByRole("tab", { name: "Users", exact: true });
  const recovery = tabs.getByRole("tab", { name: "Deleted Users" });
  await expect(tabs).toHaveAttribute("aria-orientation", "vertical");
  await expect(page.getByRole("heading", { name: "Users and roles" })).toBeVisible();
  await active.focus();
  await page.keyboard.press("ArrowDown");
  await expect(recovery).toBeFocused();
  await expect(active).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("tabpanel", { name: "Deleted Users" })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 720 });
  await expect(tabs).toHaveAttribute("aria-orientation", "horizontal");
  await expect(recovery).toBeFocused();
  await expect(recovery).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowRight");
  await expect(tabs.getByRole("tab", { name: "Deleted Reviews" })).toBeFocused();
});

test("admin animates new panels and disclosures only when motion is allowed", async ({ page }) => {
  await signIn(page);
  await page.addInitScript(() => {
    window.adminTransitions = [];
    document.addEventListener("transitionrun", (event) => {
      window.adminTransitions.push(`${event.target.id}:${event.pseudoElement}:${event.propertyName}`);
    });
  });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto(`${base}/admin#ai`);
  await expect(page.locator("#admin-ai-root")).toBeVisible();
  await page.getByRole("tab", { name: "Users", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.adminTransitions)).toContain("users-root::opacity");

  const add = page.locator("#add-user-summary");
  const form = page.getByRole("form", { name: "Add user" });
  const transition = await add.evaluate(async (summary) => {
    const details = summary.parentElement;
    const started = new Promise((resolve) => details.addEventListener("toggle", () => {
      resolve(details.getAnimations().some((item) => item.effect.getKeyframes()[0].height));
    }, { once: true }));
    summary.click();
    return started;
  });
  expect(transition).toBe(true);
  await expect(form).toBeVisible();
  await add.focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  await expect(form).toBeHidden();
  await expect(page.getByRole("searchbox")).toBeFocused();

  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.getByRole("tab", { name: "Prompts", exact: true }).click();
  const prompts = page.locator("#admin-prompts-root");
  await expect(prompts).toHaveCSS("transition-duration", "0s");
  const reduced = await prompts.locator("details.gys-admin-disclosure").first().evaluate(async (details) => {
    const toggled = new Promise((resolve) => details.addEventListener("toggle", () => resolve(details.getAnimations().length), { once: true }));
    details.querySelector("summary").click();
    return toggled;
  });
  expect(reduced).toBe(0);
});

test("admin text fields use a single focus glow and keep a high contrast outline", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/admin#scorecards`);
  const field = page.getByRole("form", { name: "New template" }).getByRole("textbox", { name: "Template name" });
  await expect(field).toBeVisible();
  const resting = await field.evaluate((el) => getComputedStyle(el).borderColor);
  await page.getByRole("tabpanel", { name: "Scorecards" }).focus();
  await page.keyboard.press("Tab");
  await expect(field).toBeFocused();
  await expect(field).toHaveCSS("outline-style", "none");
  await expect(field).not.toHaveCSS("border-color", resting);
  await expect(field).toHaveCSS("box-shadow", /0px 0px 0px 3px/);

  await page.emulateMedia({ forcedColors: "active" });
  await expect(field).toHaveCSS("outline-style", "solid");
  await expect(field).toHaveCSS("outline-width", "2px");
});

test("admin user directory keeps the search and focus after editing, deleting and restoring", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/admin#users`);
  const add = page.locator("#add-user-summary");
  const nickname = `Directory-${randomUUID().slice(0, 8)}`;
  const search = page.getByRole("searchbox", { name: "Search by name or email" });
  await expect(search).toBeVisible();
  await expect(page.getByLabel(/Nickname/)).toBeHidden();
  await add.focus();
  await page.keyboard.press("Enter");
  await page.getByLabel(/Nickname/).fill(nickname);
  await page.getByRole("button", { name: "Add user", exact: true }).click();
  await expect(add).toBeFocused();
  await search.fill(nickname);
  const row = page.locator("#users-list li").filter({ hasText: nickname });
  const summary = row.locator("summary");
  await expect(page.locator("#users-list li:visible")).toHaveCount(1);
  await expect(page.locator("#users-count")).toHaveText(/1 of \d+ accounts/);
  await expect(row.getByRole("button", { name: "Save roles" })).toBeHidden();
  await summary.focus();
  await page.keyboard.press("Space");
  await row.getByRole("checkbox", { name: "QA", exact: true }).check();
  await row.getByRole("button", { name: "Save roles" }).click();
  await expect(row.locator("summary").getByText("QA", { exact: true })).toBeVisible();
  await expect(search).toHaveValue(nickname);
  await expect(summary).toBeFocused();
  await page.keyboard.press("Enter");
  page.once("dialog", (dialog) => dialog.accept());
  await row.getByRole("button", { name: "Delete user" }).click();
  await expect(page.getByText("No accounts match your search.")).toBeVisible();
  await expect(search).toBeFocused();

  await page.setViewportSize({ width: 390, height: 720 });
  await page.getByRole("tab", { name: "Deleted Users" }).click();
  const deleted = page.getByRole("region", { name: "Deleted Users table" });
  await expect(deleted.getByRole("rowheader", { name: nickname })).toBeVisible();
  await deleted.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => deleted.evaluate((el) => el.scrollLeft)).toBeGreaterThan(0);
  await deleted.getByRole("button", { name: `Restore ${nickname}` }).click();
  await expect(deleted.getByRole("rowheader", { name: nickname })).toHaveCount(0);
  await page.getByRole("tab", { name: "Users", exact: true }).click();
  await expect(summary).toBeVisible();
});

test("AI settings keeps optional guidance folded and can reset saved empty routing", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/admin#ai`);
  const guidance = page.getByText("Supported JSON fields", { exact: true });
  const provider = page.getByRole("form", { name: "OpenRouter provider routing" });
  const reset = provider.getByRole("button", { name: "Reset to default" });
  await expect(guidance).toBeVisible();
  await expect(page.getByText(/order \/ only \/ ignore/)).toBeHidden();
  await guidance.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText(/order \/ only \/ ignore/)).toBeVisible();
  await provider.getByRole("textbox", { name: "Provider routing JSON" }).fill("{}");
  await provider.getByRole("button", { name: "Save routing" }).click();
  await expect(reset).toBeVisible();
  await expect(provider.getByRole("textbox", { name: "Provider routing JSON" })).toHaveValue("{}");
  page.once("dialog", (dialog) => dialog.accept());
  await reset.click();
  await expect(reset).toHaveCount(0);
});

test("prompt history is on demand and a stored version can be read in full", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/admin#prompts`);
  const scoring = page.getByRole("region", { name: "AI Scoring" });
  const content = "Prompt example. ".repeat(15) + randomUUID();
  await scoring.getByLabel("Prompt content").fill(content);
  await scoring.getByRole("button", { name: "Save as active version" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Saved as the active version." })).toBeVisible();
  const history = scoring.locator("summary").filter({ hasText: "Version history" });
  await expect(scoring.locator("pre").first()).toBeHidden();
  await history.focus();
  await page.keyboard.press("Enter");
  const version = scoring.locator("summary").filter({ hasText: "Read full prompt" }).first();
  await version.focus();
  await page.keyboard.press("Enter");
  await expect(scoring.locator("pre").first()).toBeVisible();
  await expect(scoring.locator("pre").first()).toHaveText(content);
});
