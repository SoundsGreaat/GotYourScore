const { expect, test } = require("@playwright/test");
const { base, nextResponse, signIn } = require("./harness.cjs");

test("AI settings only updates the saved or reset card and keeps its disclosure and size", async ({ page }) => {
  await signIn(page);
  await page.setViewportSize({ width: 390, height: 720 });
  await page.goto(`${base}/admin#ai`);

  const provider = page.getByRole("region", { name: "OpenRouter provider routing" });
  const routing = provider.getByLabel("Provider routing JSON");
  const guidance = provider.locator("details");
  const saveRouting = provider.getByRole("button", { name: "Save routing" });
  const resetRouting = provider.getByRole("button", { name: "Reset to default" });
  const request = page.getByRole("region", { name: "OpenRouter model and reasoning" });
  const model = request.getByLabel("Model override");
  const effort = request.getByRole("button", { name: "Reasoning effort" });
  const saveRequest = request.getByRole("button", { name: "Save model settings" });
  const resetRequest = request.getByRole("button", { name: "Reset to defaults" });
  const defaultEffort = "Model default (no override)";
  const providerDefault = await routing.inputValue();

  await guidance.locator("summary").press("Enter");
  await routing.evaluate((textarea) => { textarea.style.height = "250px"; });
  await model.fill("test/unsaved-model");
  await effort.press("ArrowDown");
  await page.keyboard.press("h");
  await expect(request.getByRole("menuitemradio", { name: "High", exact: true })).toBeFocused();
  await page.keyboard.press("Enter");

  await routing.fill("{");
  const failed = nextResponse(page, "/admin/ai/provider", "POST");
  await saveRouting.press("Enter");
  expect((await failed).status()).toBe(400);
  await expect(page.getByRole("alert").filter({ hasText: /^Request error$/ })).toBeVisible();
  await expect(routing).toHaveValue("{");
  await expect(saveRouting).toBeFocused();

  // Save an explicit copy of the default. Reset must still clear a dirty
  // textarea even though the server-rendered default value doesn't change.
  await routing.fill(providerDefault);
  await saveRouting.press("Enter");
  await expect(page.getByRole("alert").filter({ hasText: "Provider routing saved." })).toBeVisible();
  await expect(resetRouting).toBeVisible();
  await expect(saveRouting).toBeFocused();
  await expect(model).toHaveValue("test/unsaved-model");
  await expect(effort).toHaveText("High");

  await routing.fill('{"sort":"price"}');
  page.once("dialog", (dialog) => dialog.accept());
  await resetRouting.press("Enter");
  await expect(resetRouting).toHaveCount(0);
  await expect(saveRouting).toBeFocused();
  await expect(routing).toHaveValue(providerDefault);
  await expect(model).toHaveValue("test/unsaved-model");
  await expect(effort).toHaveText("High");

  await routing.fill('{"sort":"price"}');
  await model.fill("");
  await saveRequest.press("Enter");
  await expect(page.getByRole("alert").filter({ hasText: "AI request settings saved." })).toBeVisible();
  await expect(resetRequest).toBeVisible();
  await expect(saveRequest).toBeFocused();
  await model.fill("test/another-draft");
  page.once("dialog", (dialog) => dialog.accept());
  await resetRequest.press("Enter");
  await expect(resetRequest).toHaveCount(0);
  await expect(saveRequest).toBeFocused();
  await expect(model).toHaveValue("");
  await expect(effort).toHaveText(defaultEffort);
  await effort.press("ArrowDown");
  const defaultOption = request.getByRole("menuitemradio", { name: defaultEffort });
  await expect(defaultOption).toBeFocused();
  await expect(defaultOption).toHaveAttribute("aria-checked", "true");
  await page.keyboard.press("Escape");
  await expect(effort).toBeFocused();
  await expect(routing).toHaveValue('{"sort":"price"}');
  await expect(routing).toHaveCSS("height", "250px");
  await expect(guidance).toHaveJSProperty("open", true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test("AI provider save preserves JSON typed while the request is pending", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/admin#ai`);
  const provider = page.getByRole("region", { name: "OpenRouter provider routing" });
  const routing = provider.getByLabel("Provider routing JSON");
  const save = provider.getByRole("button", { name: "Save routing" });

  let releaseSave;
  const held = new Promise((resolve) => { releaseSave = resolve; });
  const path = "/admin/ai/provider";
  await page.route(`**${path}`, async (route) => {
    await held;
    await route.continue();
  }, { times: 1 });

  await routing.fill("{}");
  const sent = page.waitForRequest(`**${path}`);
  try {
    await save.press("Enter");
    await sent;
    await routing.fill('{"sort":"price"}');
    await page.keyboard.press("Tab"); // Draft should survive even if the textarea loses focus.
    await expect(save).toBeFocused();
  } finally {
    releaseSave();
  }
  await expect(page.getByRole("alert").filter({ hasText: "Provider routing saved." })).toBeVisible();
  await expect(routing).toHaveValue('{"sort":"price"}');
  const reset = provider.getByRole("button", { name: "Reset to default" });
  page.once("dialog", (dialog) => dialog.accept());
  await reset.press("Enter");
  await expect(reset).toHaveCount(0);
});
