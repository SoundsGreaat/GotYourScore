const { expect, test } = require("@playwright/test");

test.skip(process.env.BUG_REPORT_BROWSER_ENABLED !== "1", "Requires the synthetic tests/browser.env fixture");

const draft = {
  title: "Demo filter keeps old results",
  steps: "1. Open the demo list.\n2. Choose the Archived filter.",
  actual: "The demo list still shows active items.",
  expected: "Only archived demo items are shown.",
  environment: "Chromium on desktop",
};
const prepared = {
  title: "Demo filter ignores Archived <tag>",
  body: "## Steps to reproduce\n\n1. Choose Archived.\n\n<img src=\"unauthorized.png\">\n" + "x".repeat(450) + "\n",
  token: "a".repeat(43),
};
const issue = { number: 42, url: "https://github.com/Example/Project/issues/42" };
const privateDetail = "private-fixture@example.invalid <b>unexpected detail</b>";

async function openDashboard(page, handlers = {}) {
  const { base, signIn } = require("./harness.cjs");
  const calls = { preview: [], publish: [] };
  for (const [operation, url] of Object.entries({
    preview: `${base}/api/bug-reports/preview`,
    publish: `${base}/api/bug-reports`,
  })) {
    await page.route(url, async (route) => {
      const request = route.request();
      calls[operation].push({
        method: request.method(),
        headers: request.headers(),
        data: request.postDataJSON(),
      });
      if (handlers[operation]) return handlers[operation](route, calls[operation].length);
      if (operation === "preview") return route.fulfill({ json: prepared });
      return route.fulfill({ status: 201, json: issue });
    });
  }
  await signIn(page);
  expect((await page.goto(`${base}/#qa-tracker/bad-feedback`)).status()).toBe(200);
  const opener = page.getByRole("button", { name: "Report a bug" });
  await expect(opener).toBeVisible();
  return { opener, dialog: page.getByRole("dialog", { name: "Report a bug" }), calls };
}

async function fillDraft(dialog) {
  await dialog.getByLabel(/^Title/).fill(draft.title);
  await dialog.getByLabel(/^Steps to reproduce/).fill(draft.steps);
  await dialog.getByLabel(/^What happened/).fill(draft.actual);
  await dialog.getByLabel(/^What you expected/).fill(draft.expected);
  await dialog.getByLabel(/^Browser and device/).fill(draft.environment);
}

function expectJsonPost(call, data) {
  expect(call).toMatchObject({
    method: "POST",
    headers: { "content-type": "application/json", "x-bug-report": "1" },
    data,
  });
  expect(call.data).toEqual(data);
}

test("keyboard users review the exact public payload before separately publishing it", async ({ page }) => {
  let releasePreview;
  let releasePublish;
  const previewHeld = new Promise((resolve) => { releasePreview = resolve; });
  const publishHeld = new Promise((resolve) => { releasePublish = resolve; });
  const { opener, dialog, calls } = await openDashboard(page, {
    preview: async (route, count) => {
      if (count === 1) await previewHeld;
      await route.fulfill({ json: prepared });
    },
    publish: async (route) => {
      await publishHeld;
      await route.fulfill({ status: 201, json: issue });
    },
  });

  const title = dialog.getByLabel(/^Title/);
  const previewButton = dialog.getByRole("button", { name: "Preview report" });
  const publishButton = dialog.getByRole("button", { name: "Publish to GitHub" });
  const previewHeading = dialog.getByRole("heading", { name: "2. Review the public issue" });
  try {
    await opener.press("Enter");
    await expect(title).toBeFocused();
    await expect(title).toHaveAccessibleDescription(/AI privacy check.*public GitHub repository.*screenshots/);
    await page.keyboard.press("Shift+Tab");
    await expect(dialog.getByRole("button", { name: "Close report" })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(opener).toBeFocused();
    await opener.press("Space");

    await previewButton.press("Enter");
    await expect(title).toBeFocused();
    expect(calls.preview).toHaveLength(0);
    await fillDraft(dialog);
    await previewButton.press("Enter");
    await expect(dialog.getByRole("status")).toHaveText(/Checking your report/);
    await expect(previewButton).toBeDisabled();
    await expect(previewButton).toBeFocused();
    await page.keyboard.press("Enter");
    releasePreview();

    await expect(previewHeading).toBeFocused();
    expect(calls.preview).toHaveLength(1);
    expectJsonPost(calls.preview[0], draft);
    expect(calls.publish).toHaveLength(0);
    expect(await dialog.locator("#bug-report-issue-title").textContent()).toBe(prepared.title);
    expect(await dialog.locator("#bug-report-issue-body").textContent()).toBe(prepared.body);
    await expect(dialog.locator("img, #bug-report-issue-body a")).toHaveCount(0);

    await page.keyboard.press("Tab");
    await expect(dialog.getByRole("button", { name: "Back", exact: true })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(dialog.getByRole("heading", { name: "1. Describe the bug" })).toBeFocused();
    await expect(title).toHaveValue(draft.title);
    await expect(dialog.getByLabel(/^Steps to reproduce/)).toHaveValue(draft.steps);
    await expect(publishButton).toHaveCount(0);

    await previewButton.press("Enter");
    await expect(previewHeading).toBeFocused();
    await publishButton.press("Enter");
    await expect(dialog.getByRole("status")).toHaveText("Publishing to GitHub…");
    await expect(publishButton).toBeDisabled();
    await page.keyboard.press("Enter");
    await page.keyboard.press("Escape");
    await expect(opener).toBeFocused();
    await opener.press("Enter");
    await expect(dialog.getByRole("status")).toHaveText("Publishing to GitHub…");
    releasePublish();

    await expect(dialog.getByRole("heading", { name: "Bug report published" })).toBeFocused();
    expect(calls.publish).toHaveLength(1);
    expectJsonPost(calls.publish[0], { token: prepared.token });
    const link = dialog.getByRole("link", { name: /View issue #42 on GitHub/ });
    await expect(link).toHaveAttribute("href", issue.url);
    await expect(link).toHaveAttribute("rel", "noopener noreferrer");
    await page.keyboard.press("Tab");
    await expect(link).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(dialog.getByRole("button", { name: "Done" })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(opener).toBeFocused();
    await opener.press("Enter");
    await expect(title).toHaveValue("");
  } finally {
    releasePreview();
    releasePublish();
  }
});

test("the mobile dark-mode form keeps actionable validation and never shows upstream error content", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  const { opener, dialog } = await openDashboard(page, {
    preview: (route, count) => {
      if (count === 1) return route.fulfill({
        status: 422, json: {
          code: "needs_details", fields: ["steps", "actual", privateDetail], detail: privateDetail,
        },
      });
      if (count === 2) return route.fulfill({
        status: 503, json: { code: "unknown-fixture-code", body: privateDetail, detail: privateDetail },
      });
      if (count === 3) return route.fulfill({
        status: 429, headers: { "Retry-After": "60" },
        json: { code: "unknown-fixture-code", detail: privateDetail },
      });
      if (count === 4) return route.fulfill({
        status: 429, headers: { "Retry-After": "not-a-retry-window" },
        json: { code: "rate_limited", detail: privateDetail },
      });
      return route.fulfill({ json: prepared });
    },
  });
  await opener.click();
  await expect(dialog).toBeVisible();
  await page.mouse.click(2, 2);
  await expect(dialog).toBeVisible();
  const horizontalOverflow = () => dialog.evaluate((node) => Math.max(
    node.scrollWidth - node.clientWidth,
    document.documentElement.scrollWidth - document.documentElement.clientWidth,
  ));
  expect(await horizontalOverflow()).toBeLessThanOrEqual(0);
  await fillDraft(dialog);

  const previewButton = dialog.getByRole("button", { name: "Preview report" });
  const steps = dialog.getByLabel(/^Steps to reproduce/);
  const actual = dialog.getByLabel(/^What happened/);
  const summary = dialog.getByRole("group", { name: "Preview not created" });
  await dialog.getByLabel(/^Title/).focus();
  await page.keyboard.press("Tab");
  await expect(steps).toBeFocused();
  await expect(steps).toHaveCSS("outline-style", "none");
  await expect(steps).toHaveCSS("box-shadow", /0px 0px 0px 3px/);
  await page.emulateMedia({ forcedColors: "active" });
  await expect(steps).toHaveCSS("outline-style", "solid");
  await expect(steps).toHaveCSS("outline-width", "2px");
  await page.emulateMedia({ forcedColors: "none" });
  await expect(steps).toHaveAccessibleDescription(/open the Demo list, then select Archived/);
  await previewButton.press("Enter");
  await expect(summary).toBeFocused();
  await expect(summary).toHaveAccessibleDescription(/isn’t enough detail to reproduce.*Nothing was published/);
  await expect(summary.getByRole("link")).toHaveCount(2);
  await expect(dialog.getByText(privateDetail)).toHaveCount(0);
  const previousHash = new URL(page.url()).hash;
  await summary.getByRole("link", { name: "Steps to reproduce" }).press("Enter");
  await expect(steps).toBeFocused();
  expect(new URL(page.url()).hash).toBe(previousHash);
  await expect(steps).toHaveAttribute("aria-invalid", "true");
  await expect(steps).toHaveAccessibleDescription(/Include the starting screen and the actions in order/);
  await steps.fill(draft.steps + "\n3. Read the list.");
  await expect(steps).not.toHaveAttribute("aria-invalid");
  await expect(steps).not.toHaveAccessibleDescription(/Include the starting screen/);
  await expect(summary.getByRole("link")).toHaveCount(1);
  await actual.fill("The demo list still shows active items after choosing Archived.");
  await expect(summary).toBeHidden();

  await previewButton.press("Enter");
  await expect(dialog.getByRole("alert")).toHaveText("Unable to preview report: Server error.");
  await expect(dialog.getByText("unknown-fixture-code")).toHaveCount(0);
  await expect(dialog.getByText(privateDetail)).toHaveCount(0);

  await previewButton.press("Enter");
  await expect(dialog.getByRole("alert")).toHaveText("Too many preview requests. Wait about 1 minute before trying again.");
  await previewButton.press("Enter");
  await expect(dialog.getByRole("alert")).toHaveText("Too many preview requests. Wait a little while before trying again.");
  await expect(dialog.getByText("not-a-retry-window")).toHaveCount(0);
  await expect(dialog.getByText(privateDetail)).toHaveCount(0);

  await page.keyboard.press("Escape");
  await expect(opener).toBeFocused();
  await opener.press("Enter");
  await expect(steps).toHaveValue(draft.steps + "\n3. Read the list.");
  await previewButton.press("Enter");
  await expect(dialog.getByRole("heading", { name: "2. Review the public issue" })).toBeFocused();
  expect(await horizontalOverflow()).toBeLessThanOrEqual(0);
});

test("failed or unconfirmed publication preserves the draft and requires a fresh preview", async ({ page }) => {
  const { opener, dialog, calls } = await openDashboard(page, {
    publish: (route, count) => {
      if (count === 1) return route.fulfill({ status: 409, json: { code: "expired_preview", detail: privateDetail } });
      if (count === 2) return route.fulfill({ status: 502, json: { code: "github_unavailable", detail: privateDetail } });
      if (count === 3) return route.fulfill({ status: 502, json: { code: "publication_uncertain", detail: privateDetail } });
      return route.fulfill({ status: 201, json: { number: issue.number, url: "https://invalid.example/issues/42" } });
    },
  });
  await opener.click();
  await fillDraft(dialog);
  const title = dialog.getByLabel(/^Title/);
  const previewButton = dialog.getByRole("button", { name: "Preview report" });
  const publishButton = dialog.getByRole("button", { name: "Publish to GitHub" });
  const alert = dialog.getByRole("alert");
  const back = dialog.getByRole("button", { name: "Back", exact: true });

  for (const [index, expected] of [
    /This preview has expired/,
    /Check the GitHub repository for your issue before trying again/,
    /Check the GitHub repository for your issue before trying again/,
    /Check the GitHub repository for your issue before trying again/,
  ].entries()) {
    await previewButton.click();
    await publishButton.click();
    await expect(alert).toHaveText(expected);
    await expect(publishButton).toBeDisabled();
    await expect(dialog.getByText(privateDetail)).toHaveCount(0);
    await expect(dialog.getByRole("link", { name: /View issue/ })).toHaveCount(0);
    await page.keyboard.press("Enter");
    await back.click();
    await expect(title).toHaveValue(draft.title);
    expect(calls.publish).toHaveLength(index + 1);
  }
});

test("an explicit retry after transport loss sends only the same preview token", async ({ page }) => {
  const { opener, dialog, calls } = await openDashboard(page, {
    publish: (route, count) => count === 1
      ? route.abort("failed")
      : route.fulfill({ status: 201, json: issue }),
  });
  await opener.click();
  await fillDraft(dialog);
  await dialog.getByRole("button", { name: "Preview report" }).click();
  await dialog.getByRole("button", { name: "Publish to GitHub" }).click();
  const retry = dialog.getByRole("button", { name: "Retry publication" });
  await expect(retry).toBeFocused();
  await expect(retry).toHaveAccessibleDescription(/retry the same confirmed preview.*cannot be published twice/);
  expect(calls.publish).toHaveLength(1);

  await retry.press("Enter");
  await expect(dialog.getByRole("heading", { name: "Bug report published" })).toBeFocused();
  await expect(dialog.getByRole("link", { name: /View issue #42/ })).toHaveAttribute("href", issue.url);
  expect(calls.preview).toHaveLength(1);
  expect(calls.publish).toHaveLength(2);
  for (const call of calls.publish) expectJsonPost(call, { token: prepared.token });
});

test("a timed-out publish request warns that GitHub may already have the issue", async ({ page }) => {
  await page.clock.install();
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const { opener, dialog } = await openDashboard(page, {
    publish: async (route) => {
      await held;
      await route.abort().catch(() => {});
    },
  });
  try {
    await opener.click();
    await fillDraft(dialog);
    await dialog.getByRole("button", { name: "Preview report" }).click();
    const publish = dialog.getByRole("button", { name: "Publish to GitHub" });
    await publish.click();
    await expect(dialog.getByRole("status")).toHaveText("Publishing to GitHub…");
    await page.clock.fastForward(70_001);
    await expect(dialog.getByRole("alert")).toHaveText(/Check the GitHub repository or retry the same confirmed preview/);
    await expect(dialog.getByRole("button", { name: "Retry publication" })).toBeEnabled();
    await expect(dialog.getByRole("button", { name: "Back", exact: true })).toBeEnabled();
  } finally {
    release();
  }
});
